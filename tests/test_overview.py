from datetime import datetime, timedelta, timezone

from bridge.agents import AgentsState
from bridge.config import load
from bridge.models import GitState, Handoff, LiveSession, ScheduledRun, SessionRecord
from bridge.overview import (
    RECENT_LIMIT,
    SCHEDULE_FAILURE_LIMIT,
    UP_NEXT_LIMIT,
    build_overview,
)
from bridge.store import Store


def _cfg(tmp_path):
    return load({"db_path": tmp_path / "overview.db", "spool_dir": tmp_path / "spool"})


def _ended(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_attention_ladder_orders_kinds_and_pins_correct_hrefs(tmp_path):
    """Ladder ordering (handoff -> running -> stale -> schedule_failure) and
    the exact context-sensitive primary action for each kind, including the
    exact interpolated project id -- a mutation that swapped hrefs or ids
    must fail this."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000

    handoff_id = store.upsert_project("/p/handoff", "handoff-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoff", next_prompt="keep going",
        summary="finish the thing", created_at=now,
    ), handoff_id)

    running_id = store.upsert_project("/p/running", "running-project")

    stale_id = store.upsert_project("/p/stale", "stale-project")

    failure_target_id = store.upsert_project("/p/failure-target", "failure-target")
    store.restore_scheduled_run(ScheduledRun(
        id="run-failed", project_path="/p/failure-target", prompt="do the thing",
        mode="interactive", scheduled_for=now - 100, created_at=now - 200,
        status="failed", completed_at=now - 50, error="boom",
    ))

    def probe(path: str) -> GitState:
        if path == "/p/stale":
            return GitState(
                status="ok", branch="main", dirty_count=3,
                oldest_uncommitted_at=now - 100 * 3600,
            )
        return GitState(status="ok", branch="main", dirty_count=0)

    def agents_fn() -> AgentsState:
        return AgentsState(status="ok", sessions=[LiveSession(
            session_id="live-1", cwd="/p/running", kind="interactive", status="busy",
        )])

    model = build_overview(store, cfg, now=now, probe_fn=probe, agents_fn=agents_fn)

    kinds = [item.kind for item in model.attention]
    assert kinds == ["handoff", "running", "stale", "schedule_failure"]

    handoff_item, running_item, stale_item, failure_item = model.attention

    assert handoff_item.project_id == handoff_id
    assert handoff_item.primary_action.label == "Continue in Terminal"
    assert handoff_item.primary_action.href == f"/project/{handoff_id}?tab=current"

    assert running_item.project_id == running_id
    assert running_item.primary_action.label == "Open project"
    assert running_item.primary_action.href == f"/project/{running_id}"

    assert stale_item.project_id == stale_id
    assert stale_item.primary_action.label == "Review project state"
    assert stale_item.primary_action.href == f"/project/{stale_id}"

    assert failure_item.project_id == failure_target_id
    assert failure_item.primary_action.label == "Review scheduled run"
    assert failure_item.primary_action.href == "/schedule"

    store.close()


def test_schedule_failure_excludes_already_retried_originals(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    store.upsert_project("/p/retried", "retried-project")
    store.upsert_project("/p/unretried", "unretried-project")

    # The original failure: its status stays "failed" forever once retried.
    store.restore_scheduled_run(ScheduledRun(
        id="orig", project_path="/p/retried", prompt="p", mode="interactive",
        scheduled_for=now - 500, created_at=now - 600, status="failed",
        completed_at=now - 400, error="boom",
    ))
    # The retry: a fresh row that references the original via `retry_of`.
    store.restore_scheduled_run(ScheduledRun(
        id="retry-1", project_path="/p/retried", prompt="p", mode="interactive",
        scheduled_for=now + 100, created_at=now - 300, status="pending",
        retry_of="orig",
    ))
    # A genuinely unretried failure must still surface.
    store.restore_scheduled_run(ScheduledRun(
        id="unretried", project_path="/p/unretried", prompt="p",
        mode="interactive", scheduled_for=now - 500, created_at=now - 600,
        status="failed", completed_at=now - 350, error="oops",
    ))

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    failure_run_ids = [item.meta["run_id"] for item in model.attention
                       if item.kind == "schedule_failure"]
    assert failure_run_ids == ["unretried"]

    store.close()


def test_schedule_failure_limit_and_newest_completed_first(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    completed_ats = [100, 500, 300, 700, 200, 600, 400]  # 7 > SCHEDULE_FAILURE_LIMIT
    for i, completed_at in enumerate(completed_ats):
        path = f"/p/fail{i}"
        store.upsert_project(path, f"fail-project-{i}")
        store.restore_scheduled_run(ScheduledRun(
            id=f"fail-{i}", project_path=path, prompt="p", mode="interactive",
            scheduled_for=now - 1000, created_at=now - 1000, status="failed",
            completed_at=completed_at, error="boom",
        ))

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    failures = [item for item in model.attention if item.kind == "schedule_failure"]
    assert len(failures) == SCHEDULE_FAILURE_LIMIT
    completed_order = [item.meta["run_id"] for item in failures]
    expected_order = [
        f"fail-{i}" for i, _ in sorted(
            enumerate(completed_ats), key=lambda p: p[1], reverse=True,
        )
    ][:SCHEDULE_FAILURE_LIMIT]
    assert completed_order == expected_order

    store.close()


def test_recent_excludes_projects_already_in_attention(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    handoff_id = store.upsert_project("/p/handoff", "handoff-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoff", next_prompt="keep going", created_at=now,
    ), handoff_id)
    store.upsert_project("/p/quiet1", "quiet-1")
    store.upsert_project("/p/quiet2", "quiet-2")

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert handoff_id in [item.project_id for item in model.attention]
    assert handoff_id not in [row.project_id for row in model.recent]

    store.close()


def test_recent_truncates_to_limit_and_pin_sorts_first(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000

    pinned_id = store.upsert_project("/p/pinned", "pinned")
    store.set_project_pinned(pinned_id, True)
    store.upsert_session(SessionRecord(
        session_id="s-pinned", transcript_path="/t/pinned", ended_at=_ended(5),
    ), pinned_id)

    for i in range(6):
        store.upsert_project(f"/p/quiet{i}", f"quiet-{i}")

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    # 7 quiet/pinned projects seeded, none of them attention-worthy; `recent`
    # still truncates to RECENT_LIMIT.
    assert len(model.recent) == RECENT_LIMIT
    assert model.recent[0].project_id == pinned_id
    assert model.recent[0].pinned is True
    assert model.recent[0].status_word == "recent"

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
    # `<time>`'s pre-JS/no-JS fallback: a readable UTC string plus an ISO
    # `datetime` attribute, not the bare epoch int a screen reader or a
    # JS-disabled load would otherwise see.
    assert model.up_next[0].scheduled_for_utc
    assert "UTC" in model.up_next[0].scheduled_for_utc
    assert model.up_next[0].scheduled_for_iso is not None

    store.close()


def test_last_session_age_floors_at_zero_under_clock_skew(tmp_path):
    """A poll/write race or clock skew can put a session's `ended_at` at or
    after the `now` Overview is built with; `last_session_age_seconds` must
    floor at 0 rather than go negative (which would render as "-2m ago")."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000

    project_id = store.upsert_project("/p/skewed", "skewed-project")
    store.upsert_session(SessionRecord(
        session_id="s-skewed", transcript_path="/t/skewed", ended_at=_ended(5),
    ), project_id)

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    # `_ended(5)` is five minutes before *real* wall-clock time, whose epoch
    # is far larger than this test's artificial `now=10_000` -- exactly the
    # "ended >= now" skew this guards against.
    assert len(model.recent) == 1
    assert model.recent[0].last_session_age_seconds == 0

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
