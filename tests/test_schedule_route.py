"""Schedule read model (Task 4.1).

Model section only -- the `/schedule` route lands in Task 4.2, which will add
a second section to this same file exercising it over HTTP.
"""

from bridge.config import load
from bridge.models import ScheduledRun
from bridge.schedule_view import ScheduleModel, build_schedule
from bridge.store import Store


def _cfg(tmp_path):
    return load({"db_path": tmp_path / "schedule.db", "spool_dir": tmp_path / "spool"})


def _run(**kwargs) -> ScheduledRun:
    defaults = dict(
        project_path="/p/proj", prompt="do the thing", mode="interactive",
    )
    defaults.update(kwargs)
    return ScheduledRun(**defaults)


def test_unknown_view_defaults_to_upcoming(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    store.upsert_project("/p/proj", "proj")

    model = build_schedule(store, view="zzz")
    assert model.view == "upcoming"

    model = build_schedule(store)
    assert model.view == "upcoming"

    model = build_schedule(store, view="history")
    assert model.view == "history"


def test_upcoming_groups_attention_then_pending_then_launching(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    store.upsert_project("/p/proj", "proj")

    # Attention: two terminal failures needing a human, newest-completed-first.
    store.restore_scheduled_run(_run(
        id="fail-old", scheduled_for=100, created_at=50,
        status="failed", completed_at=200, error="boom",
    ))
    store.restore_scheduled_run(_run(
        id="fail-new", scheduled_for=150, created_at=90,
        status="missed", completed_at=300,
    ))
    # A retried original must be excluded from attention -- only its retry
    # (still terminal-failed here) may appear.
    store.restore_scheduled_run(_run(
        id="fail-retried", scheduled_for=120, created_at=60,
        status="failed", completed_at=250, error="boom",
    ))
    store.restore_scheduled_run(_run(
        id="fail-retry-of-it", scheduled_for=400, created_at=260,
        status="indeterminate", completed_at=500, retry_of="fail-retried",
    ))
    # A cancelled run is terminal but not "needs attention".
    store.restore_scheduled_run(_run(
        id="cancelled-1", scheduled_for=130, created_at=70,
        status="cancelled", completed_at=260,
    ))

    # Pending: chronological by scheduled_for ascending.
    store.create_scheduled_run(_run(
        id="pending-later", scheduled_for=900, created_at=10,
    ))
    store.create_scheduled_run(_run(
        id="pending-earlier", scheduled_for=800, created_at=10,
    ))

    # Launching.
    store.restore_scheduled_run(_run(
        id="launching-1", scheduled_for=700, created_at=10,
        status="launching", claimed_at=690,
    ))

    model = build_schedule(store, view="upcoming")
    assert model.view == "upcoming"

    attention_ids = [row.id for row in model.attention]
    assert "fail-retried" not in attention_ids
    assert attention_ids == ["fail-retry-of-it", "fail-new", "fail-old"]
    assert "cancelled-1" not in attention_ids

    pending_ids = [row.id for row in model.pending]
    assert pending_ids == ["pending-earlier", "pending-later"]

    launching_ids = [row.id for row in model.launching]
    assert launching_ids == ["launching-1"]

    assert model.history == []


def test_retryable_flag_and_scheduled_for_utc_populated(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    store.upsert_project("/p/proj", "proj")

    store.restore_scheduled_run(_run(
        id="fail-1", scheduled_for=100, created_at=50,
        status="failed", completed_at=200, error="boom",
    ))
    store.restore_scheduled_run(_run(
        id="fail-retried", scheduled_for=120, created_at=60,
        status="failed", completed_at=250,
    ))
    store.restore_scheduled_run(_run(
        id="fail-retry-of-it", scheduled_for=400, created_at=260,
        status="indeterminate", completed_at=500, retry_of="fail-retried",
    ))
    store.restore_scheduled_run(_run(
        id="fired-1", scheduled_for=90, created_at=40,
        status="fired", completed_at=190, fired_at=190,
    ))

    model = build_schedule(store, view="upcoming")
    by_id = {row.id: row for row in model.attention}

    assert by_id["fail-1"].retryable is True
    assert by_id["fail-1"].scheduled_for_utc != ""
    assert by_id["fail-1"].mode == "interactive"
    # The retried original is excluded from attention entirely (asserted in
    # the grouping test); its retry is retryable in turn since nothing has
    # retried *it*.
    assert by_id["fail-retry-of-it"].retryable is True

    # A fired (successful) run is neither attention-worthy nor retryable.
    assert "fired-1" not in by_id


def test_history_view_paginates_terminal_rows_with_total(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    store.upsert_project("/p/proj", "proj")

    # 5 terminal rows plus 1 active (pending) row that must never appear in
    # history. completed_at spaced so newest-first ordering is unambiguous.
    for i in range(5):
        store.restore_scheduled_run(_run(
            id=f"term-{i}", scheduled_for=1000 + i, created_at=10,
            status="fired", completed_at=100 + i, fired_at=100 + i,
        ))
    store.create_scheduled_run(_run(id="still-pending", scheduled_for=2000))

    page0 = build_schedule(store, view="history", page=0, page_size=2)
    assert page0.view == "history"
    assert page0.history_total == 5
    assert page0.page == 0
    assert page0.page_size == 2
    assert len(page0.history) == 2
    # Newest-completed-first: term-4 (completed_at=104) then term-3 (103).
    assert [row.id for row in page0.history] == ["term-4", "term-3"]

    page1 = build_schedule(store, view="history", page=1, page_size=2)
    assert [row.id for row in page1.history] == ["term-2", "term-1"]
    assert page1.history_total == 5

    assert page0.attention == []
    assert page0.pending == []
    assert page0.launching == []

    # No pending/launching row ever surfaces in history.
    all_history_ids = {row.id for row in page0.history} | {row.id for row in page1.history}
    assert "still-pending" not in all_history_ids


def test_build_schedule_returns_frozen_model(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    model = build_schedule(store)
    assert isinstance(model, ScheduleModel)
    try:
        model.page = 5
        assert False, "ScheduleModel should be frozen"
    except AttributeError:
        pass
