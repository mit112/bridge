from datetime import datetime, timedelta, timezone

from bridge.cards import build_cards
from bridge.config import load
from bridge.models import AgentsState, GitState, Handoff, LiveSession, SessionRecord
from bridge.store import Store
from bridge.workspace import build_workspace


def _cfg(tmp_path):
    return load({"db_path": tmp_path / "workspace.db", "spool_dir": tmp_path / "spool"})


def _ended(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_unknown_project_id_returns_none(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)

    model = build_workspace(
        store, cfg, 999999, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is None
    store.close()


def test_queued_handoff_surfaces_and_matches_the_cards_handoff(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/handoff", "handoff-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoff", next_prompt="keep going",
        summary="finish the thing", created_at=1,
    ), pid)

    model = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert model.handoff is not None
    assert model.handoff["id"] == "h1"
    assert model.handoff == model.card.handoff
    store.close()


def test_card_identity_matches_a_direct_build_cards_call(tmp_path):
    """The workspace's `card` must be the same projection Overview/Projects
    render -- not a second, divergently-built Card -- so its fields must
    match a direct `build_cards` call filtered to this project id."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/one", "one-project")
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", title="a session",
        ended_at=_ended(5),
    ), pid)

    def agents_fn():
        return AgentsState(status="ok", sessions=[])

    model = build_workspace(store, cfg, pid, "current", agents_fn=agents_fn)
    assert model is not None
    assert model.card.project_id == pid

    reference_cards = build_cards(
        store, cfg,
        probe_fn=lambda _p: GitState(status="unavailable"),
        agents_fn=agents_fn, debouncer=None, hook_state=None,
    )
    reference = next(c for c in reference_cards if c.project_id == pid)

    assert model.card.name == reference.name
    assert model.card.path == reference.path
    assert model.card.session == reference.session
    assert model.card.launch_models == reference.launch_models
    assert model.card.launch_efforts == reference.launch_efforts
    assert model.card.launch_permission_modes == reference.launch_permission_modes
    store.close()


def test_unknown_tab_normalizes_to_current(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/tab", "tab-project")

    model = build_workspace(
        store, cfg, pid, "zzz",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert model.tab == "current"
    store.close()


def test_missing_tab_normalizes_to_current(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/tab2", "tab-project-2")

    model = build_workspace(
        store, cfg, pid, None,
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert model.tab == "current"
    store.close()


def test_git_comes_from_the_cache_not_a_fresh_probe(tmp_path):
    """No `probe_fn` is injected here, so if `build_workspace` ever spawned a
    live probe (rather than relying on `build_cards`' cache fallback and this
    module's own `store.get_git_cache` read) it would hit the real, unpatched
    `gitprobe.probe` against a path that does not exist and fail outright
    rather than quietly returning the cached state."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/does-not-exist-on-disk", "cached-git")
    store.put_git_cache(
        pid,
        GitState(status="ok", branch="main", dirty_count=3,
                  oldest_uncommitted_at=100),
        probed_at=500,
    )

    model = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert model.git is not None
    assert model.git.cached_at == 500
    assert model.git.branch == "main"
    assert model.git.dirty_count == 3
    store.close()


def test_no_git_cache_yields_none_git(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/no-cache", "no-cache-project")

    model = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert model.git is None
    store.close()


def test_sessions_tab_populates_only_sessions_list(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/sessions", "sessions-project")
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", title="first",
        ended_at=_ended(10),
    ), pid)
    store.upsert_session(SessionRecord(
        session_id="s2", transcript_path="/t/s2", title="second",
        ended_at=_ended(5),
    ), pid)
    store.create_handoff(Handoff(
        id="h1", project_path="/p/sessions", next_prompt="next", created_at=1,
    ), pid)

    model = build_workspace(
        store, cfg, pid, "sessions",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert len(model.sessions) == 2
    assert model.handoffs == []
    assert model.launches == []
    store.close()


def test_handoffs_tab_populates_only_handoffs_list(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/handoffs-tab", "handoffs-tab-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoffs-tab", next_prompt="next", created_at=1,
    ), pid)
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", ended_at=_ended(5),
    ), pid)

    model = build_workspace(
        store, cfg, pid, "handoffs",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert len(model.handoffs) == 1
    assert model.sessions == []
    assert model.launches == []
    store.close()


def test_current_tab_leaves_history_lists_empty(tmp_path):
    """The Current tab draws its handoff/session view off `card`, not the
    50-capped history reads -- those stay unfetched (empty) when unselected."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/current", "current-project")
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", ended_at=_ended(5),
    ), pid)

    model = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert model.sessions == []
    assert model.handoffs == []
    assert model.launches == []
    store.close()


def test_session_metas_reads_only_for_populated_sessions_list(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/meta", "meta-project")
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", ended_at=_ended(5),
    ), pid)
    (cfg.session_meta_dir).mkdir(parents=True, exist_ok=True)
    (cfg.session_meta_dir / "s1.json").write_text(
        '{"session_id": "s1", "files_modified": 3}'
    )

    model = build_workspace(
        store, cfg, pid, "sessions",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert "s1" in model.session_metas
    assert model.session_metas["s1"].files_modified == 3
    store.close()


def test_live_state_is_threaded_through_without_a_second_probe(tmp_path):
    """Passing `live_state` directly (mirroring `DashboardBuilder.full_update`)
    must be reflected on the card without requiring `agents_fn`."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/live", "live-project")

    live_state = AgentsState(status="ok", sessions=[LiveSession(
        session_id="live-1", cwd="/p/live", kind="interactive", status="busy",
    )])

    model = build_workspace(store, cfg, pid, "current", live_state=live_state)

    assert model is not None
    assert model.card.live is not None
    assert model.card.live.session_id == "live-1"
    store.close()


def test_hidden_project_yields_none(tmp_path):
    """A project row can exist while `build_cards` (which reads active
    projects only) never produces a card for it -- no card means no
    workspace."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/hidden", "hidden-project")
    store.set_project_status(pid, "hidden")

    model = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is None
    store.close()
