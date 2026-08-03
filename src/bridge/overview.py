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
from bridge.models import AgentsState, Card
from bridge.refresh import RefreshCoordinator
from bridge.store import Store, now_epoch, to_epoch

# Overview shows only the highest-value subset; the full lists live on
# Projects/Schedule. Small enough to read in one glance, per the spec's "calm"
# requirement for this page.
RECENT_LIMIT = 5
UP_NEXT_LIMIT = 3

# Terminal, non-cancelled scheduled-run statuses: something Bridge promised to
# do and did not, as opposed to `fired` (it happened) or `cancelled` (the user
# said not to).
SCHEDULE_FAILURE_STATUSES = ("failed", "indeterminate", "missed")


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


@dataclass(frozen=True)
class ScheduleRow:
    """One row of the Overview's "up next" list."""

    id: str
    project_id: int | None
    project_name: str | None
    prompt_preview: str
    scheduled_for: int
    status: str
    error: str | None = None


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
) -> OverviewModel:
    """Assemble the Overview.

    `live_state`/`cards`/`now` are accepted verbatim so a caller that already
    ran one poll cycle (e.g. the route building both the live envelope and
    Overview together) can pass its results straight through with no second
    probe. `probe_fn`/`agents_fn` exist only for test determinism (mirroring
    `build_cards`); when `cards`/`live_state` are omitted, the one probe this
    function performs is threaded into `DashboardBuilder` rather than letting
    it probe again on its own.
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
        cards = build_cards(store, cfg, probe_fn=probe_fn, agents_fn=lambda: live_state)

    coordinator = RefreshCoordinator(store, cfg)
    builder = DashboardBuilder(
        store, cfg, coordinator, probe_fn=probe_fn, agents_fn=agents_fn,
        now_fn=lambda: now,
    )
    envelope = builder.full_update(live_state=live_state, cards=cards, now=now)

    attention = _attention_from_cards(cards) + _schedule_failures(store)
    recent = [_project_summary(card, now) for card in cards[:RECENT_LIMIT]]
    up_next = [
        _schedule_row(row, store)
        for row in store.scheduled_runs(status="pending")[:UP_NEXT_LIMIT]
    ]

    return OverviewModel(
        attention=attention,
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
        if card.handoff:
            items.append(AttentionItem(
                kind="handoff",
                project_id=card.project_id,
                title=card.name,
                summary=(
                    card.handoff.get("summary")
                    or card.handoff.get("next_prompt", "")
                ),
                primary_action=Action(
                    "Continue in Terminal", f"/project/{card.project_id}?tab=current",
                ),
                meta={"handoff_id": card.handoff.get("id")},
            ))
        elif card.live is not None:
            items.append(AttentionItem(
                kind="running",
                project_id=card.project_id,
                title=card.name,
                summary=f"Session {card.live.status}",
                primary_action=Action(
                    "Open project", f"/project/{card.project_id}",
                ),
                meta={"live_status": card.live.status},
            ))
        elif card.is_stale:
            items.append(AttentionItem(
                kind="stale",
                project_id=card.project_id,
                title=card.name,
                summary=f"{card.git.dirty_count} uncommitted change(s)",
                primary_action=Action(
                    "Review project state", f"/project/{card.project_id}",
                ),
                meta={"dirty_count": card.git.dirty_count},
            ))
    return items


def _schedule_failures(store: Store) -> list[AttentionItem]:
    """Terminal, non-cancelled scheduled runs: a promise Bridge did not keep.

    Appended after the per-project ladder rather than interleaved into it: a
    schedule failure is not a property of any one card's rank, so it has no
    natural position in `sort_key`'s ordering.
    """
    out: list[AttentionItem] = []
    for row in store.scheduled_runs():
        if row["status"] not in SCHEDULE_FAILURE_STATUSES:
            continue
        project = store.project_by_path(row["project_path"])
        title = project["name"] if project is not None else row["project_path"]
        summary = f"Scheduled run {row['status']}"
        if row["error"]:
            summary = f"{summary}: {row['error']}"
        out.append(AttentionItem(
            kind="schedule_failure",
            project_id=project["id"] if project is not None else None,
            title=title,
            summary=summary,
            primary_action=Action("Review scheduled run", "/schedule"),
            meta={"run_id": row["id"], "status": row["status"]},
        ))
    return out


def _project_summary(card: Card, now: int) -> ProjectSummary:
    ended = to_epoch(card.session.ended_at) if card.session else None
    return ProjectSummary(
        project_id=card.project_id,
        name=card.name,
        path=card.path,
        status_word=_status_word(card),
        branch=card.git.branch,
        dirty_count=card.git.dirty_count,
        last_session_title=card.session.title if card.session else None,
        last_session_age_seconds=(now - ended) if ended is not None else None,
        tokens_today=card.tokens_today,
        tokens_5h=card.tokens_5h,
        pinned=card.pinned,
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


def _schedule_row(row, store: Store) -> ScheduleRow:
    project = store.project_by_path(row["project_path"])
    return ScheduleRow(
        id=row["id"],
        project_id=project["id"] if project is not None else None,
        project_name=project["name"] if project is not None else None,
        prompt_preview=(row["prompt"] or "")[:120],
        scheduled_for=row["scheduled_for"],
        status=row["status"],
        error=row["error"],
    )
