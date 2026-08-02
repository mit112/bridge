import threading
import time

import pytest

from bridge.__main__ import _shutdown_scheduler, main


def test_index_subcommand_runs_and_reports(tmp_path, capsys):
    projects = tmp_path / "projects"
    (projects / "-Users-mitsheth-dev-demo").mkdir(parents=True)
    code = main(["index", "--projects-dir", str(projects),
                 "--db", str(tmp_path / "b.db"),
                 "--spool-dir", str(tmp_path / "spool")])
    assert code == 0
    assert "files_seen" in capsys.readouterr().out


def test_unknown_subcommand_is_an_error(tmp_path):
    assert main(["nonsense"]) == 2


@pytest.fixture
def serve_cfg(tmp_path, monkeypatch):
    """A `serve` whose uvicorn never runs, over a throwaway `~/.bridge`.

    `main` has no `--launches-dir`, so the config is replaced wholesale. That is
    also the point of the exercise: without it the autouse guard fires, because
    a `serve` under test would otherwise garbage-collect the developer's real
    prompt files.
    """
    from bridge import __main__ as entry
    from bridge.config import load

    launches = tmp_path / "launches"
    launches.mkdir()
    cfg = load({"db_path": tmp_path / "s.db", "spool_dir": tmp_path / "spool",
                "launches_dir": launches,
                "claude_projects_dir": tmp_path / "projects"})
    monkeypatch.setattr(entry, "load", lambda overrides: cfg)
    served = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.append(kw))
    return launches, served


def test_serve_collects_stale_prompt_files_before_it_starts(serve_cfg):
    """The only recurring event under the manual-`bridge serve` uptime model.

    Nothing called `gc_prompt_files`, so `~/.bridge/launches` grew forever. It
    is deliberately NOT called from `create_app`: the suite builds apps directly
    with configs that still point `launches_dir` at the real `~/.bridge`, so a
    boot-time collector there would delete real provenance during tests.
    """
    launches, served = serve_cfg
    stale = launches / "old.prompt"
    stale.write_text("what ran two months ago")
    old = time.time() - 20 * 86400
    import os
    os.utime(stale, (old, old))
    fresh = launches / "new.prompt"
    fresh.write_text("what ran this morning")

    assert main(["serve"]) == 0
    assert served, "uvicorn must still be reached"
    assert not stale.exists(), "20 days is past the 14-day policy"
    assert fresh.exists(), "a live prompt file is provenance, not litter"


def test_an_uncollectable_launches_dir_does_not_stop_the_panel(serve_cfg,
                                                               monkeypatch):
    """Housekeeping is never the reason the panel refuses to start.

    Same policy as the boot drain, and the same reason: the collector is a
    convenience and the server is the point.
    """
    launches, served = serve_cfg
    from bridge import launcher

    def boom(*a, **k):
        raise OSError("launches dir is unreadable")

    monkeypatch.setattr(launcher, "gc_prompt_files", boom)
    assert main(["serve"]) == 0
    assert served, "the failure must not have reached uvicorn"


def test_serve_prunes_finished_scheduled_runs_past_the_retention_window(serve_cfg,
                                                                        tmp_path):
    """Startup is the only housekeeping event this process has -- the same
    reason `gc_prompt_files` lives here -- so it is where finished scheduled
    runs are reaped. Only finished ones: an ancient `pending` job is still a
    launch the panel owes you.
    """
    from bridge.models import ScheduledRun
    from bridge.store import SCHEDULED_RUN_RETENTION_DAYS, Store, now_epoch

    s = Store(tmp_path / "s.db")
    for jid in ("ancient", "recent"):
        s.create_scheduled_run(ScheduledRun(
            id=jid, project_path="/p", prompt="x", mode="terminal",
            scheduled_for=1000, created_at=1000))
        s.claim_specific(jid)
        s.finish_scheduled_run(jid, status="fired")
    s.create_scheduled_run(ScheduledRun(
        id="ancient-pending", project_path="/p", prompt="x", mode="terminal",
        scheduled_for=1000, created_at=1000))
    long_ago = now_epoch() - (SCHEDULED_RUN_RETENTION_DAYS + 1) * 86400
    s.conn.execute("UPDATE scheduled_runs SET completed_at=? WHERE id='ancient'",
                   (long_ago,))
    s.conn.commit()
    s.close()

    assert main(["serve"]) == 0

    s = Store(tmp_path / "s.db")
    assert s.get_scheduled_run("ancient") is None
    assert s.get_scheduled_run("recent") is not None
    assert s.get_scheduled_run("ancient-pending") is not None
    s.close()


class _FakeThread:
    """A `threading.Thread` double whose `is_alive()` is set by the test,
    independent of whether `join()` actually blocked -- what makes a
    30-second-timeout race exercisable without waiting 30 real seconds."""

    def __init__(self, alive: bool):
        self._alive = alive
        self.joined_with: float | None = None

    def join(self, timeout=None):
        self.joined_with = timeout

    def is_alive(self):
        return self._alive


class _FakeStore:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_shutdown_does_not_close_the_store_while_the_thread_is_still_alive():
    """A tick mid-launch when the join times out must not have its connection
    yanked out from under it. `join_timeout` is set tiny here -- not the real
    30s -- since this is exercising the guard, not the timeout duration."""
    stop = threading.Event()
    t = _FakeThread(alive=True)
    store = _FakeStore()

    _shutdown_scheduler(stop, t, store, join_timeout=0.01)

    assert stop.is_set()
    assert t.joined_with == 0.01
    assert not store.closed, "closing while the thread is alive is the race"


def test_shutdown_closes_the_store_once_the_thread_has_terminated():
    stop = threading.Event()
    t = _FakeThread(alive=False)
    store = _FakeStore()

    _shutdown_scheduler(stop, t, store, join_timeout=0.01)

    assert store.closed
