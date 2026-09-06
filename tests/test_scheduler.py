import pytest

from bridge.config import load
from bridge.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "sub" / "t.db")
    yield s
    s.close()


@pytest.fixture
def cfg(tmp_path):
    return load({
        "db_path": tmp_path / "s.db",
        "spool_dir": tmp_path / "sp",
        "launches_dir": tmp_path / "l",
    })


def _job(store, sid="j1", scheduled_for=1000, **kw):
    from bridge.models import ScheduledRun

    job = ScheduledRun(id=sid, project_path="/p", prompt="go", mode="background",
                       scheduled_for=scheduled_for, created_at=500, **kw)
    return store.create_scheduled_run(job)


def test_tick_fires_due_jobs_and_records_launch_id(store, cfg):
    _job(store, "a", scheduled_for=1000)

    def fake(store, cfg, spec, handoff_id=None, **kw):
        from bridge.launcher import LaunchResult

        return LaunchResult("L1", "started")

    from bridge import scheduler

    assert scheduler.tick(store, cfg, fake, now=1500) == 1
    assert store.get_scheduled_run("a")["status"] == "fired"
    assert store.get_scheduled_run("a")["launch_id"] == "L1"


def test_tick_marks_a_returned_failure_failed_not_crashed(store, cfg):
    _job(store, "a", scheduled_for=1000)

    def fake(store, cfg, spec, handoff_id=None, **kw):
        from bridge.launcher import LaunchResult

        return LaunchResult("L1", "failed", error="boom")

    from bridge import scheduler

    scheduler.tick(store, cfg, fake, now=1500)
    r = store.get_scheduled_run("a")
    assert r["status"] == "failed" and r["error"] == "boom"


def test_tick_survives_one_raising_job_and_still_fires_the_next(store, cfg):
    _job(store, "a", scheduled_for=1000)
    _job(store, "b", scheduled_for=1001)
    calls = {"n": 0}

    def fake(store, cfg, spec, handoff_id=None, **kw):
        from bridge.launcher import LaunchResult, LaunchError

        calls["n"] += 1
        if calls["n"] == 1:
            raise LaunchError("bad")  # pre-spawn raise
        return LaunchResult("L2", "started")

    from bridge import scheduler

    scheduler.tick(store, cfg, fake, now=1500)
    assert store.get_scheduled_run("a")["status"] == "failed"
    assert store.get_scheduled_run("b")["status"] == "fired"


def test_tick_records_indeterminate_on_an_unexpected_exception_and_still_fires_next(
    store, cfg
):
    """A non-`LaunchError` exception -- a bug, or a `sqlite3.IntegrityError`
    from a handoff deleted between schedule and fire -- must not strand the
    claimed job or crash the loop for whatever else is due right now."""
    _job(store, "a", scheduled_for=1000)
    _job(store, "b", scheduled_for=1001)
    calls = {"n": 0}

    def fake(store, cfg, spec, handoff_id=None, **kw):
        from bridge.launcher import LaunchResult

        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return LaunchResult("L2", "started")

    from bridge import scheduler

    assert scheduler.tick(store, cfg, fake, now=1500) == 2
    a = store.get_scheduled_run("a")
    assert a["status"] == "indeterminate"
    assert "boom" in a["error"]
    assert store.get_scheduled_run("b")["status"] == "fired"


def test_tick_never_fires_future_or_cancelled(store, cfg):
    _job(store, "a", scheduled_for=5000)
    store.cancel_pending(_job(store, "b", scheduled_for=1000))

    from bridge import scheduler

    assert scheduler.tick(store, cfg, (lambda *a, **k: None), now=1500) == 0


def test_a_job_half_an_hour_late_still_fires(store, cfg):
    """The catch-up window is what makes a closed lid or a restarted panel
    harmless: the run is still owed and the user is still expecting it."""
    _job(store, "a", scheduled_for=10_000)
    calls = []

    def fake(store, cfg, spec, handoff_id=None, **kw):
        from bridge.launcher import LaunchResult

        calls.append(spec)
        return LaunchResult("L1", "started")

    from bridge import scheduler

    assert scheduler.tick(store, cfg, fake, now=10_000 + 1800) == 1
    assert store.get_scheduled_run("a")["status"] == "fired"
    assert len(calls) == 1


def test_a_job_two_hours_late_is_missed_and_never_launched(store, cfg):
    """Firing retroactively would spawn a session at an unpredictable moment
    for work the user may have forgotten scheduling -- the refusal
    `schedspool.rebuild_if_empty` already makes after a database loss."""
    from bridge import schedspool, scheduler

    _job(store, "stale", scheduled_for=10_000)
    _job(store, "due", scheduled_for=10_000 + 7000)
    calls = []

    def fake(store, cfg, spec, handoff_id=None, **kw):
        from bridge.launcher import LaunchResult

        calls.append(spec.prompt)
        return LaunchResult("L1", "started")

    when = 10_000 + 7200
    assert scheduler.tick(store, cfg, fake, now=when) == 1

    stale = store.get_scheduled_run("stale")
    assert stale["status"] == "missed"
    assert stale["completed_at"] == when
    assert stale["launch_id"] is None
    assert len(calls) == 1, "only the in-window job may fire"
    assert store.get_scheduled_run("due")["status"] == "fired"

    # Journalled, so a database loss replays it as missed rather than as owed.
    record = cfg.spool_dir / "schedules" / f"stale.{when}.status.json"
    assert record.exists()
    assert schedspool._load_status(record).status == "missed"


def test_a_missed_job_is_still_retryable(store, cfg):
    """`missed` is terminal, not a dead end: recovery is an explicit retry,
    and that is the only reason refusing to fire it is acceptable."""
    from bridge import scheduler

    _job(store, "stale", scheduled_for=10_000)
    scheduler.tick(store, cfg, (lambda *a, **k: None), now=10_000 + 7200)

    row = store.retry_terminal("stale", new_id="retry-1")

    assert row is not None
    assert row["status"] == "launching"
    assert row["retry_of"] == "stale"
