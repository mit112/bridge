"""Project workspace read model: the tabbed `/project/{id}` surface.

Assembled from the same sources Overview/Projects use -- `cards.build_cards`
for the project's `Card` (never a second, divergent git or liveness probe) and
`store.get_git_cache` for the "as of ... ago" git panel, exactly as the
pre-redesign `detail` route in `api.py` already reads it. History
(`sessions`/`handoffs`/`launches`) stays capped at the existing default of 50,
newest first, and only the selected tab's list is populated -- the other two
are empty lists, never fetched, since a tab a user is not viewing has no
reason to pay for a query.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass

from bridge import sessionmeta
from bridge.cards import build_cards
from bridge.config import Config
from bridge.models import AgentsState, Card, GitState
from bridge.store import Store

# Exactly the tab vocabulary the workspace route accepts. Anything else
# (missing, unknown, typo'd) normalizes to "current" -- there is no blank tab.
VALID_TABS = ("current", "sessions", "handoffs", "launches")
DEFAULT_TAB = "current"


@dataclass(frozen=True)
class WorkspaceModel:
    project: sqlite3.Row
    # The single `cards.Card` for this project -- the same projection
    # Overview/Projects render, reused rather than re-derived. Launch option
    # catalogs (`launch_models`/`launch_efforts`/`launch_permission_modes`)
    # live on this card already; the workspace does not duplicate them.
    card: Card
    # The queued handoff, or None. Reused off `card.handoff` rather than a
    # second `store.queued_handoff` call.
    handoff: dict | None
    # The git panel's cache-with-`cached_at` view, read explicitly via
    # `store.get_git_cache` to mirror the pre-redesign `detail` route's "as of
    # ... ago" rendering. In this module's call configuration `card.git`
    # happens to carry the same cached value (both ultimately read the same
    # cache row) -- this field exists so the git panel has its own named,
    # obviously-cache-sourced read rather than reaching into the card for it.
    git: GitState | None
    tab: str
    sessions: list[sqlite3.Row]
    handoffs: list[sqlite3.Row]
    launches: list[sqlite3.Row]
    session_metas: dict[str, sessionmeta.SessionMeta]
    # Paging for the selected history tab. `history_total` is the true count of
    # that tab's rows (sessions/handoffs/launches) BEFORE the page slice, so the
    # template can state "showing X-Y of N" and offer prev/next -- the capped-at-
    # 50 disclosure was only ever half the fix. On the Current tab, which draws
    # off `card` and fetches no history, all three stay at their defaults.
    history_total: int = 0
    page: int = 0
    page_size: int = 50
    # The launches tab needs a linked launch's session TITLE, but `sessions`
    # above stays empty unless `tab == "sessions"` -- fetching all 50 of a
    # project's sessions just to resolve a handful of launch->session joins
    # would be the "other tab's data, unfetched" rule broken for the sake of
    # this one column. A per-id `store.session_row` read, one per DISTINCT
    # linked session on this page (at most as many as there are launches),
    # is the narrower read the join actually needs.
    launch_sessions: dict[str, sqlite3.Row] = dataclasses.field(default_factory=dict)


def _normalize_tab(tab: str | None) -> str:
    return tab if tab in VALID_TABS else DEFAULT_TAB


def build_workspace(
    store: Store,
    cfg: Config,
    project_id: int,
    tab: str,
    *,
    page: int = 0,
    page_size: int = 50,
    live_state: AgentsState | None = None,
    probe_fn=None,
    agents_fn=None,
    git_cache=None,
) -> WorkspaceModel | None:
    """Assemble the workspace for one project, or None for an unknown id.

    `probe_fn` defaults to a cache-reading stand-in (`GitState(status=
    "unavailable")`), the same trick `DashboardBuilder.live_patch` uses, so a
    page view never spawns a live git probe for every project -- `build_cards`
    falls back to each project's git cache instead. `live_state`, when given,
    takes precedence over `agents_fn` (mirroring `DashboardBuilder`, where an
    injected `live_state` always wins): a caller that already polled liveness
    once passes it straight through instead of paying for a second probe.

    `git_cache` is forwarded so this page reads the same window every other
    route does. It composes with the stand-in rather than replacing it: the
    stand-in still guarantees no live probe from here, so the cache serves what
    it has and the refreshes this page schedules can only ever come back
    `unavailable`, which writes nothing. `/` and `/projects`, which pass a real
    `probe_fn`, are what actually keep the rows current.
    """
    row = store.get_project(project_id)
    if row is None:
        return None

    tab = _normalize_tab(tab)

    if live_state is not None:
        agents_fn = lambda: live_state  # noqa: E731 - trivial, injected closure
    # else: use the caller's `agents_fn` verbatim (None -> `build_cards`'
    # own default, `agents.probe`).

    cards = build_cards(
        store,
        cfg,
        probe_fn=(probe_fn or (lambda _p: GitState(status="unavailable"))),
        agents_fn=agents_fn,
        debouncer=None,
        hook_state=None,
        git_cache=git_cache,
    )
    card = next((c for c in cards if c.project_id == project_id), None)
    if card is None:
        # A project row can exist (e.g. hidden/archived) while `build_cards`
        # -- which reads `store.projects()` with `include_hidden=False` --
        # never produces a card for it. No card means no workspace to render.
        return None

    cached_git = store.get_git_cache(project_id)
    git: GitState | None = None
    if cached_git is not None:
        git, probed_at = cached_git
        git = dataclasses.replace(git, cached_at=probed_at)

    sessions: list[sqlite3.Row] = []
    handoffs: list[sqlite3.Row] = []
    launches: list[sqlite3.Row] = []
    launch_sessions: dict[str, sqlite3.Row] = {}
    # The true total for the selected tab, so the pager can state "of N" rather
    # than repeat the old "up to 50" disclosure. Only the viewed tab is counted;
    # the other two are never fetched, same as their row lists.
    history_total = 0
    offset = page * page_size
    if tab == "sessions":
        sessions = store.sessions(project_id, limit=page_size, offset=offset)
        history_total = store.count_sessions(project_id)
    elif tab == "handoffs":
        handoffs = store.handoffs(project_id, limit=page_size, offset=offset)
        history_total = store.count_handoffs(project_id)
    elif tab == "launches":
        launches = store.launches(project_id, limit=page_size, offset=offset)
        history_total = store.count_launches(project_id)
        for launch_row in launches:
            session_id = launch_row["session_id"]
            if session_id and session_id not in launch_sessions:
                session = store.session_row(session_id)
                if session is not None:
                    launch_sessions[session_id] = session

    session_metas = sessionmeta.read_many(
        [s["id"] for s in sessions], cfg.session_meta_dir
    )

    return WorkspaceModel(
        project=row,
        card=card,
        handoff=card.handoff,
        git=git,
        tab=tab,
        sessions=sessions,
        handoffs=handoffs,
        launches=launches,
        session_metas=session_metas,
        launch_sessions=launch_sessions,
        history_total=history_total,
        page=page,
        page_size=page_size,
    )
