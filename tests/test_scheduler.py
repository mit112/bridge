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


def test_tick_never_fires_future_or_cancelled(store, cfg):
    _job(store, "a", scheduled_for=5000)
    store.cancel_pending(_job(store, "b", scheduled_for=1000))

    from bridge import scheduler

    assert scheduler.tick(store, cfg, (lambda *a, **k: None), now=1500) == 0
