from datetime import datetime, timedelta, timezone

from bridge.agents import AgentsState
from bridge.config import load
from bridge.models import GitState, Handoff, LiveSession, ScheduledRun, SessionRecord
from bridge.overview import RECENT_LIMIT, UP_NEXT_LIMIT, build_overview
from bridge.store import Store


def _cfg(tmp_path):
    return load({"db_path": tmp_path / "overview.db", "spool_dir": tmp_path / "spool"})


def _ended(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _seed(store: Store, now: int):
    """Six projects covering every rung of the attention ladder, plus a
    pinned-but-quiet project to prove pin ordering independent of rank, and a
    failed scheduled run to prove the schedule-failure category."""
    pinned = store.upsert_project("/p/pinned", "pinned")
    store.set_project_pinned(pinned, True)
    store.upsert_session(SessionRecord(
        session_id="s-pinned", transcript_path="/t/pinned", ended_at=_ended(5),
    ), pinned)

    handoff_proj = store.upsert_project("/p/handoff", "handoff-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoff", next_prompt="keep going",
        summary="finish the thing", created_at=now,
    ), handoff_proj)

    running_proj = store.upsert_project("/p/running", "running-project")

    stale_proj = store.upsert_project("/p/stale", "stale-project")

    recent_proj = store.upsert_project("/p/recent", "recent-project")
    store.upsert_session(SessionRecord(
        session_id="s-recent", transcript_path="/t/recent", ended_at=_ended(60),
    ), recent_proj)

    idle_proj = store.upsert_project("/p/idle", "idle-project")

    # A failed scheduled run against the recent project (chosen so
    # project-resolution is exercised, not just a bare path).
    store.restore_scheduled_run(ScheduledRun(
        id="run-failed", project_path="/p/recent", prompt="do the thing",
        mode="interactive", scheduled_for=now - 100, created_at=now - 200,
        status="failed", completed_at=now - 50, error="boom",
    ))

    return {
        "pinned": pinned, "handoff": handoff_proj, "running": running_proj,
        "stale": stale_proj, "recent": recent_proj, "idle": idle_proj,
    }


def _probe_fn(stale_proj_path: str, now: int):
    def probe(path: str) -> GitState:
        if path == stale_proj_path:
            return GitState(
                status="ok", branch="main", dirty_count=3,
                oldest_uncommitted_at=now - 100 * 3600,
            )
        return GitState(status="ok", branch="main", dirty_count=0)
    return probe


def _agents_fn():
    return AgentsState(status="ok", sessions=[LiveSession(
        session_id="live-1", cwd="/p/running", kind="interactive", status="busy",
    )])


def test_attention_ladder_matches_card_priority_and_actions(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    ids = _seed(store, now)

    model = build_overview(
        store, cfg, now=now,
        probe_fn=_probe_fn("/p/stale", now),
        agents_fn=_agents_fn,
    )

    kinds = [item.kind for item in model.attention]
    assert kinds == ["handoff", "running", "stale", "schedule_failure"]

    handoff_item, running_item, stale_item, failure_item = model.attention
    assert handoff_item.project_id == ids["handoff"]
    assert handoff_item.primary_action.label == "Continue in Terminal"
    assert running_item.project_id == ids["running"]
    assert running_item.primary_action.label == "Open project"
    assert stale_item.project_id == ids["stale"]
    assert stale_item.primary_action.label == "Review project state"
    assert failure_item.primary_action.label == "Review scheduled run"
    assert failure_item.project_id == ids["recent"]

    store.close()


def test_recent_truncates_and_keeps_pin_first(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    ids = _seed(store, now)

    model = build_overview(
        store, cfg, now=now,
        probe_fn=_probe_fn("/p/stale", now),
        agents_fn=_agents_fn,
    )

    assert len(model.recent) == RECENT_LIMIT
    assert model.recent[0].project_id == ids["pinned"]
    assert model.recent[0].pinned is True
    assert model.recent[0].status_word == "recent"
    # The idle project is the 6th card; it is truncated out of `recent`.
    assert ids["idle"] not in [row.project_id for row in model.recent]

    store.close()


def test_up_next_is_chronological_and_truncated(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    store.upsert_project("/p/sched", "sched-project")
    for i, offset in enumerate([400, 100, 300, 200]):
        store.create_scheduled_run(ScheduledRun(
            id=f"pending-{i}", project_path="/p/sched", prompt="p",
            mode="interactive", scheduled_for=now + offset, created_at=now,
        ))

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert len(model.up_next) == UP_NEXT_LIMIT
    assert [row.scheduled_for for row in model.up_next] == sorted(
        row.scheduled_for for row in model.up_next
    )
    assert model.up_next[0].scheduled_for == now + 100

    store.close()


def test_totals_freshness_and_diagnostics_reuse_dashboard_envelope(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    store.upsert_project("/p/only", "only-project")
    store.record_index_run({"parse_errors": 0}, ran_at=now - 10, duration_ms=1)

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model.freshness["server"] == "available"
    assert model.freshness["index_at"] == now - 10
    assert model.totals["projects"] == 1
    assert model.diagnostics_alert is False

    store.close()


def test_no_handoff_or_schedule_failure_yields_empty_attention(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    store.upsert_project("/p/quiet", "quiet-project")

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model.attention == []

    store.close()
