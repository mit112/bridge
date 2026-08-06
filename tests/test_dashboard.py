from datetime import datetime, timedelta, timezone

from bridge.agents import AgentsState
from bridge.cards import RANK_HANDOFF
from bridge.config import load
from bridge.dashboard import DashboardBuilder
from bridge.models import GitState, Handoff, LiveSession, ScheduledRun, SessionRecord
from bridge.overview import build_overview
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


def test_the_envelope_carries_the_same_attention_count_the_overview_rendered(tmp_path):
    """The "Needs attention" cell is the Overview's headline number, and it was
    the one number on the strip that no live frame could correct -- `topbar`
    never carried it, so it stayed at its page-load value until a reload.

    Asserted as an equality with `build_overview`, not just as a literal: two
    independent counts of "needs a human" is exactly the drift worth
    forbidding, and a literal alone would let both sides move together into
    the same wrong answer.
    """
    cfg = load({"db_path": tmp_path / "attention.db", "spool_dir": tmp_path / "spool-att"})
    store = Store(cfg.db_path)
    queued = store.upsert_project("/p/queued", "queued-project")
    store.upsert_project("/p/quiet", "quiet-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/queued", next_prompt="keep going", created_at=1,
    ), queued)
    store.create_scheduled_run(ScheduledRun(
        id="sched-dead", project_path="/p/quiet", prompt="p",
        mode="terminal", scheduled_for=10,
    ))
    store.claim_one_due(now=20)
    store.finish_scheduled_run("sched-dead", status="failed", error="boom")
    store.record_index_run({"parse_errors": 0}, ran_at=100, duration_ms=1)

    probe = lambda _path: GitState(status="ok", branch="main", dirty_count=0)  # noqa: E731
    agents_fn = lambda: AgentsState(status="ok", sessions=[])  # noqa: E731
    builder = DashboardBuilder(
        store, cfg, RefreshCoordinator(store, cfg),
        probe_fn=probe, agents_fn=agents_fn, now_fn=lambda: 130,
    )

    update = builder.full_update()
    model = build_overview(store, cfg, now=130, probe_fn=probe, agents_fn=agents_fn)

    # One queued handoff, one failed scheduled run.
    assert update["topbar"]["attention"] == 2
    assert update["topbar"]["attention"] == model.attention_total
    store.close()


def test_a_quiet_project_is_not_counted_as_needing_attention(tmp_path):
    """The count has to be able to reach 0, or the cell is decoration. Pairs
    with the Overview's own "a live-but-idle session is not an attention item"
    rule: the strip must agree with the ladder about doing nothing."""
    cfg = load({"db_path": tmp_path / "calm.db", "spool_dir": tmp_path / "spool-calm"})
    store = Store(cfg.db_path)
    store.upsert_project("/p/calm", "calm-project")
    builder = DashboardBuilder(
        store, cfg, RefreshCoordinator(store, cfg),
        probe_fn=lambda _p: GitState(status="ok", branch="main", dirty_count=0),
        agents_fn=lambda: AgentsState(status="ok", sessions=[
            LiveSession(cwd="/p/calm", status="idle", started_at="1m"),
        ]),
        now_fn=lambda: 130,
    )

    assert builder.full_update()["topbar"]["attention"] == 0
    store.close()
