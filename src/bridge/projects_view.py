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
from bridge.overview import ProjectSummary, count_summary, project_summary
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
    # How many rows with THIS group's status word were pulled into Pinned
    # instead. The header renders it, because otherwise the group count and the
    # filter chip above it are two different numbers for the same word with
    # nothing on screen to reconcile them -- see `group_projects`.
    pinned_elsewhere: int = 0


# One label per status word, shared by every surface so a project reads the
# same on Projects, Overview and the detail page. The enum stays what it is
# (`stale`) -- JS filters, CSS pills and the mutation anchors all key off it --
# but no user-facing string ever says the raw word. "stale" is the state users
# flagged as opaque: it means uncommitted work sitting past `stale_hours`, so it
# reads "Uncommitted", which says what is actually true.
_STATUS_LABELS = {
    # The live sensor's statuses share this map: every other one it reports
    # (busy, working, idle, waiting, blocked, failed, ended, unknown) is a
    # single word the `.title()` default already renders correctly.
    "needs_input": "Needs input",
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

    Pinning is an ORDERING choice, not a status: a pinned project holding a
    queued handoff is still queued, and the "Queued" chip (which counts
    `count_summary`'s projects, not this group's rows) still counts it, and the
    chip's filter still reveals its row -- inside Pinned. So the chip could read
    3 while the group header under it read 2, with nothing on screen to explain
    the gap. Rather than break chip-equals-filter (the equality a click can
    actually check) or duplicate the row into two groups, each status group
    carries the number of its rows Pinned is holding, and says so in its
    header. Every number stays true and the arithmetic is visible.
    """
    buckets: dict[str, list[ProjectSummary]] = {key: [] for key, _, _ in _GROUP_ORDER}
    pinned_elsewhere: dict[str, int] = {}
    for row in rows:
        key = "pinned" if row.pinned else row.status_word
        buckets.get(key, buckets["idle"]).append(row)
        if row.pinned:
            word = row.status_word if row.status_word in buckets else "idle"
            pinned_elsewhere[word] = pinned_elsewhere.get(word, 0) + 1
    return [
        ProjectGroup(key, label, is_open, buckets[key], pinned_elsewhere.get(key, 0))
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

    # `overview.count_summary` is the panel's one count vocabulary: the chips
    # here and the Overview tiles are the same expressions, in the same unit
    # (projects), so "Running 1" here and "Running 5" there can no longer be
    # two different questions. `queued` was the last handoff-unit count on this
    # page -- it read 10 while the Queued group under it listed 2 projects.
    totals = count_summary(store, cards, live_state)
    counts = {
        "all": totals["projects"],
        "needs_attention": totals["attention"],
        "running": totals["running"],
        "queued": totals["queued"],
        "hidden": len(hidden),
    }

    return ProjectsModel(rows=rows, counts=counts, hidden=hidden)
