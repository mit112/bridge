from datetime import datetime, timedelta, timezone

from bridge.agents import AgentsState
from bridge.cards import RANK_HANDOFF
from bridge.config import load
from bridge.dashboard import DashboardBuilder
from bridge.models import GitState, Handoff, LiveSession, SessionRecord
from bridge.refresh import RefreshCoordinator, RefreshStatus
from bridge.store import Store


def test_full_update_projects_absolute_totals_and_server_order(tmp_path):
    cfg = load({"db_path": tmp_path / "dashboard.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    first = store.upsert_project("/p/first", "first")
    second = store.upsert_project("/p/second", "second")
    store.set_project_pinned(first, True)
    store.create_handoff(Handoff(id="h1", project_path="/p/second", next_prompt="keep", created_at=1), second)
    ended = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", project_path="/p/first",
        title="one", ended_at=ended, tokens_in=1000, tokens_out=500,
    ), first)
    store.record_index_run({"parse_errors": 0}, ran_at=100, duration_ms=1)
    status = RefreshStatus(generation=3, index_at=100)
    coordinator = RefreshCoordinator(store, cfg)
    builder = DashboardBuilder(
        store, cfg, coordinator,
        probe_fn=lambda path: GitState(
            status="ok", branch="main", dirty_count=3 if path == "/p/first" else 0,
        ),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
        now_fn=lambda: 130,
    )
    coordinator._status = status

    update = builder.full_update()
    assert update["generation"] == 3
    assert update["freshness"]["server"] == "available"
    assert update["freshness"]["index_at"] == 100
    assert update["topbar"]["today"] == 1500
    assert update["topbar"]["last_5h"] == 1500
    assert update["topbar"]["dirty"] == 1  # one dirty tree (/p/first), even though it has 3 dirty files
    assert update["card_order"] == [first, second]
    assert update["cards"][str(second)]["burn"]["today"] == 0
    assert RANK_HANDOFF == -1
    store.close()


def test_live_patch_does_not_include_store_leaf_fields(tmp_path):
    cfg = load({"db_path": tmp_path / "dashboard2.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/live", "live")
    coordinator = RefreshCoordinator(store, cfg)
    builder = DashboardBuilder(
        store, cfg, coordinator,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[LiveSession(
            session_id="s", cwd="/p/live", kind="interactive", status="busy",
        )]),
    )
    patch = builder.live_patch()
    assert patch["kind"] == "patch"
    assert "card_order" not in patch
    assert "git" not in patch["cards"][str(pid)]
    assert "burn" not in patch["cards"][str(pid)]
    assert "next_prompt" not in str(patch)
    store.close()
