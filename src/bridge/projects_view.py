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
