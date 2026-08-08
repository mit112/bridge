"""Projects index read model: the complete, filterable project list.

Assembled from the same source Overview reads -- `bridge.cards.build_cards`,
kept in its own `sort_key` order rather than re-sorted here -- and the same
`ProjectSummary` projection Overview uses (`bridge.overview.project_summary`),
so the two pages can never disagree about what a project's status word or
last-session age is. No new SQL: `hidden` mirrors the exact list `api.py`'s
dashboard route already builds from `store.projects(include_hidden=True)`.
"""

from __future__ import annotations

from dataclasses import dataclass

from bridge import agents
from bridge.cards import build_cards
from bridge.config import Config
from bridge.models import AgentsState, Card
from bridge.overview import ProjectSummary, project_summary
from bridge.store import Store, now_epoch


@dataclass(frozen=True)
class ProjectsModel:
    rows: list[ProjectSummary]
    counts: dict
    hidden: list[dict]


@dataclass(frozen=True)
class ProjectGroup:
    key: str
    label: str
    is_open: bool
    rows: list[ProjectSummary]


# One label per status word, shared by every surface so a project reads the
# same on Projects, Overview and the detail page. The enum stays what it is
# (`stale`) -- JS filters, CSS pills and the mutation anchors all key off it --
# but no user-facing string ever says the raw word. "stale" is the state users
# flagged as opaque: it means uncommitted work sitting past `stale_hours`, so it
# reads "Uncommitted", which says what is actually true.
_STATUS_LABELS = {
    "queued": "Queued",
    "running": "Running",
    "stale": "Uncommitted",
    "recent": "Recent",
    "idle": "Idle",
}


def status_label(word: str) -> str:
    return _STATUS_LABELS.get(word, word.title())


# The Projects index groups, in render order, with each group's default
# disclosure state. `pinned` is not a status word: a pinned project keeps its
# real status but is pulled to its own group at the top, so a row that sorts
# above the rest for a reason the user set outright reads as deliberate rather
# than mis-sorted. The active-work groups open by default; the passive tail
# (Recent, Idle) starts collapsed so a long list of quiet projects does not
# bury the two or three that need a decision now.
_GROUP_ORDER = [
    ("pinned", "Pinned", True),
    ("running", "Running", True),
    ("queued", "Queued", True),
    ("stale", "Uncommitted", True),
    ("recent", "Recent", False),
    ("idle", "Idle", False),
]


def group_projects(rows: list[ProjectSummary]) -> list[ProjectGroup]:
    """Bucket the already-sorted rows into ordered, collapsible groups.

    Rows arrive in `cards.sort_key` order and stay in it within each bucket --
    this only partitions, it never re-sorts. A pinned project lands in the
    Pinned bucket regardless of its status; every other row groups by its
    `status_word`. Empty groups are dropped so the page shows only the states
    that actually exist right now.
    """
    buckets: dict[str, list[ProjectSummary]] = {key: [] for key, _, _ in _GROUP_ORDER}
    for row in rows:
        key = "pinned" if row.pinned else row.status_word
        buckets.get(key, buckets["idle"]).append(row)
    return [
        ProjectGroup(key, label, is_open, buckets[key])
        for key, label, is_open in _GROUP_ORDER
        if buckets[key]
    ]


def build_projects(
    store: Store,
    cfg: Config,
    *,
    live_state: AgentsState | None = None,
    cards: list[Card] | None = None,
    probe_fn=None,
    agents_fn=None,
    git_cache=None,
) -> ProjectsModel:
    """Assemble the Projects index.

    `live_state`/`cards` are accepted verbatim so a caller that already ran
    one poll cycle can pass its results straight through with no second probe
    -- the same contract `build_overview` honours. `probe_fn`/`agents_fn`
    exist only for test determinism, mirroring `build_cards` itself; when
    `cards`/`live_state` are omitted, the one probe this function performs is
    threaded into `build_cards` rather than letting it probe again. `git_cache`
    is forwarded so the route's own stale-while-revalidate window covers that
    probe -- it is what keeps `/projects` off a full git sweep per request.
    """
    now = now_epoch()
    if agents_fn is None:
        agents_fn = agents.probe
    if live_state is None:
        try:
            live_state = agents_fn()
        except Exception:  # noqa: BLE001 - a broken sensor must not break Projects
            live_state = AgentsState(status="unavailable", sessions=[], source="none")
    if cards is None:
        cards = build_cards(
            store, cfg, probe_fn=probe_fn, agents_fn=lambda: live_state,
            git_cache=git_cache,
        )

    rows = [project_summary(card, now) for card in cards]

    # `store.projects()` (which `build_cards` reads from) whitelists `active`,
    # so a hidden or archived project never reaches `cards` at all -- this is
    # the one place either status is still reachable. Mirrors the exact list
    # `api.py`'s dashboard route already builds.
    hidden = [
        dict(row) for row in store.projects(include_hidden=True)
        if row["status"] != "active"
    ]

    counts = {
        "all": len(rows),
        # A project "needs attention" for the same reasons `overview.py`'s
        # attention ladder surfaces it: a queued handoff, a live session, or
        # uncommitted work stale past the threshold.
        "needs_attention": sum(
            1 for card in cards if card.handoffs or card.live is not None or card.is_stale
        ),
        # A live session on a card that also has a queued handoff renders as
        # "queued" (a handoff outranks a running session in _status_word), and
        # the Running filter matches that rendered state -- so it must not be
        # counted here either, or the Running badge would outnumber the rows the
        # Running filter shows. It is still counted under `queued` below.
        "running": sum(
            1 for card in cards if card.live is not None and not card.handoffs
        ),
        "queued": sum(len(card.handoffs) for card in cards),
        "hidden": len(hidden),
    }

    return ProjectsModel(rows=rows, counts=counts, hidden=hidden)
