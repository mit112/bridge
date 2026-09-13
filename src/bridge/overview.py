"""Overview read model: the calm "what needs my attention" surface.

Assembled entirely from sources the rest of the panel already reads: cards
from `bridge.cards.build_cards` (the same source Projects/Workspace use, kept
in its own `sort_key` order rather than re-sorted here) and the
freshness/totals/diagnostics-alert envelope from `bridge.dashboard.
DashboardBuilder`. No new SQL, and no second git or liveness probe: `cards`/
`live_state`/`now` are accepted as injection points (mirroring
`DashboardBuilder.full_update`'s own signature) so a caller sharing one poll
cycle across Overview and the live-update envelope pays for exactly one probe,
and when this function must probe for itself it does so once and threads the
result into `DashboardBuilder`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bridge import agents
from bridge.cards import build_cards
from bridge.config import Config
from bridge.dashboard import DashboardBuilder
from bridge.filters import _schedule_time_fields
from bridge.models import AgentsState, Card
from bridge.refresh import RefreshCoordinator
from bridge.registry import display_name
from bridge.store import Store, now_epoch, to_epoch

# Overview shows only the highest-value subset; the full lists live on
# Projects/Schedule. Small enough to read in one glance, per the spec's "calm"
# requirement for this page.
ATTENTION_LIMIT = 3
RECENT_LIMIT = 5
UP_NEXT_LIMIT = 3
# Terminal, non-cancelled scheduled-run statuses: something Bridge promised to
# do and did not, as opposed to `fired` (it happened) or `cancelled` (the user
# said not to).
SCHEDULE_FAILURE_STATUSES = ("failed", "indeterminate", "missed")

# How a live session's own status becomes an attention item: `kind` -> the pill
# `overview.html`'s `status_map` renders, `summary` -> the line under it.
#
# Keyed on the status rather than on `card.live is not None`, because a live
# record is not a claim of live work: the sensor reports a session that is
# merely sitting there just as readily as one mid-turn. Deriving the pill from
# existence alone rendered "Working now" directly above "Session idle", and
# counted that project in the "N items need your attention" headline.
#
# A status missing from this map is not an attention item at all -- it falls
# through to `recent`, where an inactive project belongs. That deliberately
# includes `unknown` and any value a future sensor invents:
# `agents.normalize_status` round-trips unrecognised statuses verbatim and
# `cards.LIVE_PRIORITY_DEFAULT` already ranks them WITH idle, so promoting one
# to the top of this page would be the wrong guess to make.
LIVE_ATTENTION = {
    "needs_input": ("session_input", "Waiting for your input"),
    "blocked": ("session_input", "Waiting for your input"),
    "failed": ("session_failed", "Session failed"),
    "errored": ("session_failed", "Session failed"),
    "busy": ("running", "Session busy"),
    "working": ("running", "Session working"),
}


# Which queued handoff a project leads with, most urgent first. `blocked` needs
# a person and outranks work a session could simply pick up; `parked` was
# deferred on purpose and is last. A handoff captured before `kind` existed has
# None, which is read as `next` -- that is what it was, since nothing else
# existed to be.
KIND_RANK = {"blocked": 0, "next": 1, None: 1, "parked": 2}


def _featured_handoff(handoffs: list[dict]) -> dict | None:
    """The one handoff a project's attention entry speaks for, or None.

    None when everything queued is `parked`: those were deferred deliberately,
    and a project whose only queued work is parked is not asking for anything.
    It falls through to the live/stale branches like any other project, rather
    than sitting in the attention list being ignored until the list stops being
    read at all.

    Ties keep `queued_handoffs`' newest-first order, because `sorted` is stable.
    """
    actionable = [h for h in handoffs if (h.get("kind") or "next") != "parked"]
    if not actionable:
        return None
    return sorted(actionable, key=lambda h: KIND_RANK.get(h.get("kind"), 1))[0]


@dataclass(frozen=True)
class Action:
    """A single primary action: label text + the href it goes to."""

    label: str
    href: str


@dataclass(frozen=True)
class ProjectSummary:
    """One row of the Overview's "recent projects" list.

    Wraps a `cards.Card` rather than re-deriving anything from Store: the
    fields below are exactly what a Jinja row needs to render, with no
    percentage-of-cap math (token values here are always absolute).
    """

    project_id: int
    name: str
    path: str
    status_word: str
    branch: str | None
    dirty_count: int
    last_session_title: str | None
    last_session_age_seconds: int | None
    tokens_today: int
    tokens_5h: int
    pinned: bool
    # Upstream divergence, None when the branch has no upstream. Surfaced as a
    # "Remote" column on the Projects index, mirroring the detail page's
    # workspace fact; behind takes precedence over ahead, else "Synced".
    ahead: int | None = None
    behind: int | None = None
    # `needs_attention(card)` for this row, rendered as `data-project-attention`
    # so the Projects "Needs attention" chip filters on the same predicate it
    # counts with. A row state alone cannot answer it: `running` covers both a
    # session mid-turn and one sitting idle, and only the first needs a human.
    needs_attention: bool = False


@dataclass(frozen=True)
class ScheduleRow:
    """One row of the Overview's "up next" list.

    `scheduled_for` stays the raw epoch int (the `data-scheduled-for` hook a
    later JS repaints in local time), but a `<time>` needs a readable
    pre-JS/no-JS fallback too -- `scheduled_for_utc`/`scheduled_for_iso` carry
    exactly what `bridge.filters._schedule_time_fields` computes, so a Jinja
    macro rendering this row never has to reformat an epoch itself. Defaulted so existing callers/tests
    constructing a `ScheduleRow` without them keep working.
    """

    id: str
    project_id: int | None
    project_name: str | None
    prompt_preview: str
    scheduled_for: int
    status: str
    error: str | None = None
    scheduled_for_utc: str = ""
    scheduled_for_iso: str | None = None
    # Added for the interactive Schedule page (bridge.schedule_view); the
    # Overview preview never needs either, so `build_overview`'s own
    # `_schedule_row` leaves both at their defaults.
    mode: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class AttentionItem:
    """One entry in the Overview's attention ladder.

    `kind` is one of "handoff" | "running" | "schedule_failure" | "stale".
    `project_id` is None for a schedule failure that Bridge cannot map back to
    a known project (the run's `project_path` no longer resolves).
    """

    kind: str
    project_id: int | None
    title: str
    summary: str
    primary_action: Action
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OverviewModel:
    attention: list[AttentionItem]
    # PROJECTS needing a human -- the same number the "Needs attention" tile
    # and the Projects chip show, and the number of project entries in
    # `attention` before it is sliced. Schedule failures are runs, not
    # projects, so they are counted separately rather than folded in: the page
    # headline names both units instead of adding them together.
    attention_total: int
    schedule_failures: int
    recent: list[ProjectSummary]
    up_next: list[ScheduleRow]
    totals: dict
    diagnostics_alert: bool
    freshness: dict


def build_overview(
    store: Store,
    cfg: Config,
    *,
    live_state: AgentsState | None = None,
    cards: list[Card] | None = None,
    now: int | None = None,
    probe_fn=None,
    agents_fn=None,
    git_cache=None,
    coordinator: RefreshCoordinator | None = None,
) -> OverviewModel:
    """Assemble the Overview.

    `live_state`/`cards`/`now` are accepted verbatim so a caller that already
    ran one poll cycle (e.g. the route building both the live envelope and
    Overview together) can pass its results straight through with no second
    probe. `probe_fn`/`agents_fn` exist only for test determinism (mirroring
    `build_cards`); when `cards`/`live_state` are omitted, the one probe this
    function performs is threaded into `DashboardBuilder` rather than letting
    it probe again on its own. `git_cache` is forwarded for the same reason, so
    a caller that omits `cards` does not lose the app's stale-while-revalidate
    window just by taking this path.
    """
    now = now_epoch() if now is None else now
    if agents_fn is None:
        agents_fn = agents.probe
    if live_state is None:
        try:
            live_state = agents_fn()
        except Exception:  # noqa: BLE001 - a broken sensor must not break Overview
            live_state = AgentsState(status="unavailable", sessions=[], source="none")
    if cards is None:
        cards = build_cards(
            store, cfg, probe_fn=probe_fn, agents_fn=lambda: live_state,
            git_cache=git_cache,
        )

    # The app passes its own coordinator -- the one that has actually been
    # running refreshes. A freshly constructed one has never run, so its status
    # is always the pristine default and the Overview's freshness strip could
    # never say "unavailable" while the sidebar, reading the app's real
    # coordinator, did. Defaulted rather than required so a direct caller that
    # has no coordinator (every test that only wants the model) keeps working.
    if coordinator is None:
        coordinator = RefreshCoordinator(store, cfg)
    builder = DashboardBuilder(
        store, cfg, coordinator, probe_fn=probe_fn, agents_fn=agents_fn,
        now_fn=lambda: now,
    )
    envelope = builder.full_update(live_state=live_state, cards=cards, now=now)

    # One lookup built from the cards this call already fetched, reused by
    # both schedule helpers below instead of a `store.project_by_path` round
    # trip per scheduled-run row.
    by_path = {card.path: card for card in cards}

    project_items = _attention_from_cards(cards)
    failures = _schedule_failures(store, by_path)
    all_attention = project_items + failures
    attention = all_attention[:ATTENTION_LIMIT]
    # "Needs attention" and "Recent projects" are distinct sections; a project
    # already surfaced above (as a queued handoff, a running session, stale, or
    # behind a schedule failure) is redundant to repeat here.
    attention_ids = {
        item.project_id for item in all_attention if item.project_id is not None
    }
    recent = [
        project_summary(card, now) for card in cards
        if card.project_id not in attention_ids
    ][:RECENT_LIMIT]
    up_next = [
        _schedule_row(row, by_path, store)
        for row in store.scheduled_runs(status="pending")[:UP_NEXT_LIMIT]
    ]

    return OverviewModel(
        attention=attention,
        attention_total=len(project_items),
        schedule_failures=len(failures),
        recent=recent,
        up_next=up_next,
        totals=envelope["topbar"],
        diagnostics_alert=envelope["diagnostics"]["alert"],
        freshness=envelope["freshness"],
    )


def _attention_from_cards(cards: list[Card]) -> list[AttentionItem]:
    """One item per card that needs a human, in `build_cards`' own order.

    `build_cards` already sorts pinned-first, then queued-handoff, running,
    stale (`cards.sort_key`); filtering that list down to attention-worthy
    cards without re-sorting is what keeps this ladder identical to the
    Projects/Workspace surfaces instead of drifting from it. Ordinary
    recent/idle cards are not attention items -- they surface in `recent`
    instead.
    """
    items: list[AttentionItem] = []
    for card in cards:
        featured = _featured_handoff(card.handoffs)
        if featured is not None:
            # ONE entry per project, not one per handoff. Six queued handoffs
            # on one project used to contribute six items, so the page
            # headlined 17 "items" against the Projects page's 9 projects for
            # the same state. The remaining handoffs are a magnitude on this
            # one entry (`handoff_count`), and the project page lists them.
            h = featured
            blocked = (h.get("kind") == "blocked")
            items.append(AttentionItem(
                # A blocked handoff is a different ASK, not a different style of
                # the same one: `handoff` means "a session can carry this on",
                # `blocked_handoff` means nothing can proceed until a person
                # does something. Rendering them alike is what left decisions
                # needing a human sitting in a queue labelled "ready".
                kind="blocked_handoff" if blocked else "handoff",
                project_id=card.project_id,
                title=(h.get("session_title") or card.name),
                summary=(h.get("summary") or h.get("next_prompt", "")),
                primary_action=Action(
                    "See what is needed" if blocked else "Continue in Terminal",
                    f"/project/{card.project_id}?tab=current",
                ),
                meta={
                    "handoff_id": h.get("id"),
                    "handoff_count": len(card.handoffs),
                    "project_name": card.name,
                    "created_at": h.get("created_at"),
                    "has_span": bool(card.session),
                    "branch": card.git.branch,
                    "dirty_count": card.git.dirty_count,
                    "path": card.path,
                    "kind": h.get("kind") or "next",
                },
            ))
        elif card.live is not None and card.live.status in LIVE_ATTENTION:
            kind, summary = LIVE_ATTENTION[card.live.status]
            items.append(AttentionItem(
                kind=kind,
                project_id=card.project_id,
                title=card.name,
                summary=summary,
                primary_action=Action(
                    "Open project", f"/project/{card.project_id}",
                ),
                meta={
                    "live_status": card.live.status,
                    "project_name": card.name,
                    "branch": card.git.branch,
                    "dirty_count": card.git.dirty_count,
                    "path": card.path,
                },
            ))
        elif card.is_stale:
            items.append(AttentionItem(
                kind="stale",
                project_id=card.project_id,
                title=card.name,
                summary=(f"{card.git.dirty_count} uncommitted change"
                         f"{'' if card.git.dirty_count == 1 else 's'}"),
                primary_action=Action(
                    "Review project state", f"/project/{card.project_id}",
                ),
                meta={
                    "dirty_count": card.git.dirty_count,
                    "project_name": card.name,
                    "branch": card.git.branch,
                    "path": card.path,
                },
            ))
    return items


def failed_schedule_rows(rows) -> list:
    """The scheduled runs that count as a broken promise, given every run row.

    Split out of `_schedule_failures` so the live-update envelope can count
    them without also resolving each one's project name and action — and, more
    importantly, so the count the command strip patches in is produced by this
    exact predicate rather than a second one that could drift from it.
    """
    retried = {r["retry_of"] for r in rows if r["retry_of"]}
    return [
        r for r in rows
        if r["status"] in SCHEDULE_FAILURE_STATUSES and r["id"] not in retried
    ]


# --- One count vocabulary, for every surface ---------------------------------
#
# The rule: **every headline count is a count of PROJECTS**. Overview used to
# headline "Running 5" from the registry's session list (including sessions in
# directories that are not projects at all) while Projects headlined "Running
# 1" from its card list, and "Needs attention 17" counted handoff ITEMS against
# the Projects chip's 9 PROJECTS. Three units, no label saying which.
#
# A genuinely useful second magnitude (handoffs queued, sessions running) is
# secondary text next to the tile -- `count_captions` below -- never the
# headline number. `count_summary` is the single expression both the Overview
# tiles (via `dashboard.DashboardBuilder`'s `topbar` envelope) and the Projects
# chips read, so a chip and a tile cannot drift apart again.


def needs_attention(card: Card) -> bool:
    """Does this project need a human? The one predicate, for every surface.

    Exactly the ladder `_attention_from_cards` walks, so the "Needs attention"
    tile, the Projects chip and the number of project entries the ladder
    renders are the same number by construction. A live session whose status is
    not in `LIVE_ATTENTION` (idle, unknown, ...) is deliberately not attention
    -- see that map's own note.
    """
    return bool(
        card.handoffs
        or (card.live is not None and card.live.status in LIVE_ATTENTION)
        or card.is_stale
    )


def is_running(card: Card) -> bool:
    """A project the panel renders as "Running": a live session and no queued
    handoff, matching `_status_word` (a handoff outranks a session) so the
    Running count and the Running group/filter can never disagree."""
    return card.live is not None and not card.handoffs


def count_summary(
    store: Store,
    cards: list[Card],
    live_state: AgentsState,
    *,
    scheduled_rows=None,
) -> dict:
    """Every count the panel headlines, in one place.

    Project-unit keys: `projects`, `running`, `attention`, `queued`, `dirty`.
    Secondary magnitudes, never a headline: `running_sessions`,
    `unattributed_sessions`, `queued_handoffs`, `schedule_failures`. `scheduled`
    is the one genuine run count -- runs Bridge still owes -- and the tile
    labels it "Scheduled runs" so it does not read as a project count.

    `scheduled_rows` lets a caller hand over the `store.scheduled_runs()`
    result it already has, so wiring these costs no extra query.
    """
    rows = store.scheduled_runs() if scheduled_rows is None else scheduled_rows
    # `agents.by_project`, not a `cwd in paths` test: a session started in a
    # SUBDIRECTORY of a project is attributed to that project's card, so
    # counting it as "unregistered" here would report work Bridge is already
    # showing as homeless.
    grouped = agents.by_project(
        live_state, store.alias_map(), [card.path for card in cards]
    )
    unattributed = grouped.get(agents.UNATTRIBUTED, [])
    return {
        "projects": len(cards),
        "running": sum(1 for card in cards if is_running(card)),
        "running_sessions": sum(
            len([s for s in sessions if not agents.is_terminal(s.status)])
            for key, sessions in grouped.items() if key != agents.UNATTRIBUTED
        ),
        "unattributed_sessions": len(
            [s for s in unattributed if not agents.is_terminal(s.status)]
        ),
        "attention": sum(1 for card in cards if needs_attention(card)),
        "schedule_failures": len(failed_schedule_rows(rows)),
        "queued": sum(1 for card in cards if card.handoffs),
        "queued_handoffs": sum(len(card.handoffs) for card in cards),
        "dirty": sum(1 for card in cards if card.git.dirty_count),
        "scheduled": sum(
            1 for row in rows if row["status"] in ("pending", "launching")
        ),
    }


def count_captions(totals: dict) -> dict[str, str]:
    """The secondary magnitude under each tile, or "" when there is none.

    Composed once, server-side, and put on the wire: live.js writes these
    strings into the tiles rather than re-wording them, so a live frame cannot
    phrase a count differently from the server render. Kept to one short noun
    phrase per tile -- the command strip is three cells across at 390px, so a
    sentence here would wrap the cells to ragged heights.
    """
    captions = {"running": "", "queued": "", "attention": "", "unattributed": ""}
    sessions = totals["running_sessions"]
    if sessions != totals["running"]:
        captions["running"] = f"{sessions} session{'' if sessions == 1 else 's'}"
    handoffs = totals["queued_handoffs"]
    if handoffs != totals["queued"]:
        captions["queued"] = f"{handoffs} handoff{'' if handoffs == 1 else 's'}"
    failures = totals["schedule_failures"]
    if failures:
        captions["attention"] = f"{failures} failed run{'' if failures == 1 else 's'}"
    stray = totals["unattributed_sessions"]
    if stray:
        # Its own line under the strip, not a share of the Running tile: these
        # sessions are running in directories Bridge has no project for, and
        # folding them into a project count is what made Overview say 5 while
        # Projects said 1.
        captions["unattributed"] = (
            f"{stray} session{'' if stray == 1 else 's'} running in "
            f"{'a directory' if stray == 1 else 'directories'} "
            "Bridge has no project for."
        )
    return captions


def _schedule_failures(store: Store, by_path: dict[str, Card]) -> list[AttentionItem]:
    """Terminal, non-cancelled scheduled runs: a promise Bridge did not keep.

    Appended after the per-project ladder rather than interleaved into it: a
    schedule failure is not a property of any one card's rank, so it has no
    natural position in `sort_key`'s ordering.

    Excludes any run already superseded by a retry: `retry_of` on the newer
    row is the store's own record of that, the same set `api.py`'s dashboard
    route already builds (`retried = {r["retry_of"] for r in rows if
    r["retry_of"]}`) before deciding what may still offer a Retry action.
    Without this, a failure that was already retried would re-surface here
    forever, since the original row's status never changes once retried.
    Kept complete for `attention_total`, newest-completed first. Only the final
    composed Overview list is sliced to `ATTENTION_LIMIT`, so the visible count
    stays honest even when Projects/Schedule carry the omitted rows.
    """
    failures = failed_schedule_rows(store.scheduled_runs())
    failures.sort(key=lambda r: (r["completed_at"] or 0), reverse=True)

    out: list[AttentionItem] = []
    for row in failures:
        card = by_path.get(row["project_path"])
        if card is not None:
            project_id, title = card.project_id, card.name
        else:
            project = store.project_by_path(row["project_path"])
            project_id = project["id"] if project is not None else None
            title = project["name"] if project is not None else row["project_path"]
        summary = f"Scheduled run {row['status']}"
        if row["error"]:
            summary = f"{summary}: {row['error']}"
        out.append(AttentionItem(
            kind="schedule_failure",
            project_id=project_id,
            title=title,
            summary=summary,
            primary_action=Action("Review scheduled run", "/schedule"),
            # `path` carries the hero card's dotted path footer, and this was
            # the one kind that omitted it -- so a schedule failure promoted to
            # hero (which happens whenever nothing else needs a human) rendered
            # without one. Read off the row rather than the card: `by_path` is
            # keyed BY `card.path`, so for a matched card the two are the same
            # string, and for an unmatched one the row still knows the path
            # while the card is None.
            meta={
                "run_id": row["id"],
                "status": row["status"],
                "path": row["project_path"],
            },
        ))
    return out


def project_summary(card: Card, now: int) -> ProjectSummary:
    """The one `Card` -> `ProjectSummary` projection, shared by Overview,
    Projects, and (later) the Workspace read model -- promoted from a private
    helper so a second module can import it instead of re-deriving the same
    fields from a `Card` a second way and drifting from this one."""
    ended = to_epoch(card.session.ended_at) if card.session else None
    return ProjectSummary(
        project_id=card.project_id,
        name=card.name,
        path=card.path,
        status_word=_status_word(card),
        branch=card.git.branch,
        dirty_count=card.git.dirty_count,
        last_session_title=card.session.title if card.session else None,
        # Floored the same way bridge.filters._ago/_ago_epoch floor their own
        # `now - epoch`: a poll/write race or clock skew can put `ended` at or
        # after `now`, and a negative age would render as "-2m ago" rather
        # than "0m ago".
        last_session_age_seconds=max(0, now - ended) if ended is not None else None,
        tokens_today=card.tokens_today,
        tokens_5h=card.tokens_5h,
        pinned=card.pinned,
        ahead=card.git.ahead,
        behind=card.git.behind,
        needs_attention=needs_attention(card),
    )


def _status_word(card: Card) -> str:
    if card.handoff:
        return "queued"
    if card.live is not None:
        return "running"
    if card.is_stale:
        return "stale"
    if card.session is not None:
        return "recent"
    return "idle"


def _schedule_row(row, by_path: dict[str, Card], store: Store) -> ScheduleRow:
    card = by_path.get(row["project_path"])
    if card is not None:
        project_id, project_name = card.project_id, card.name
    else:
        # Falls back to a store lookup only for a path Bridge no longer has a
        # card for (e.g. an archived project) -- the common case resolves off
        # the cards already fetched, with no per-row query.
        project = store.project_by_path(row["project_path"])
        project_id = project["id"] if project is not None else None
        # The row macro renders `project_name` unguarded, so leaving this None
        # for a run whose project resolves nowhere printed the literal "None"
        # as the project name. The path is always known, so its leaf is a real
        # answer -- and the same one indexing would have derived.
        project_name = (project["name"] if project is not None
                        else display_name(row["project_path"]))
    iso, utc = _schedule_time_fields(row["scheduled_for"])
    return ScheduleRow(
        id=row["id"],
        project_id=project_id,
        project_name=project_name,
        prompt_preview=(row["prompt"] or "")[:120],
        scheduled_for=row["scheduled_for"],
        status=row["status"],
        error=row["error"],
        scheduled_for_utc=utc,
        scheduled_for_iso=iso,
    )
