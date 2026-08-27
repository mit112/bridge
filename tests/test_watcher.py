import threading, time
from pathlib import Path
from bridge.watcher import FileWatcher

def _drain(fn, timeout=2.0):
    ev = threading.Event()
    def cb():
        fn()
        ev.set()
    return cb, ev

def test_a_new_file_triggers_on_change(tmp_path):
    calls = []
    ev = threading.Event()
    def cb():
        calls.append(1); ev.set()
    w = FileWatcher(tmp_path, on_change=cb, poll_s=0.02, quiet_s=0.02)
    w.start()
    try:
        (tmp_path / "s.jsonl").write_text("{}")
        assert ev.wait(2.0), "watcher did not fire for a new file"
    finally:
        w.stop()
    assert calls

def test_a_burst_within_the_quiet_window_coalesces_to_one_call(tmp_path):
    calls = []
    def cb(): calls.append(1)
    w = FileWatcher(tmp_path, on_change=cb, poll_s=0.02, quiet_s=0.15)
    w.start()
    try:
        f = tmp_path / "s.jsonl"
        for i in range(5):
            f.write_text("{}" * (i + 1))
            time.sleep(0.03)          # all within one quiet window's reach
        time.sleep(0.4)
    finally:
        w.stop()
    assert len(calls) == 1, f"burst should coalesce to one reindex, got {len(calls)}"

def test_stop_joins_cleanly(tmp_path):
    w = FileWatcher(tmp_path, on_change=lambda: None, poll_s=0.02)
    w.start()
    w.stop()
    assert not w.is_alive()

def test_a_raising_callback_does_not_kill_the_thread(tmp_path):
    state = {"n": 0}
    def cb():
        state["n"] += 1
        raise RuntimeError("boom")
    w = FileWatcher(tmp_path, on_change=cb, poll_s=0.02, quiet_s=0.02)
    w.start()
    try:
        (tmp_path / "a.jsonl").write_text("{}")
        time.sleep(0.3)
        (tmp_path / "b.jsonl").write_text("{}")
        time.sleep(0.3)
    finally:
        w.stop()
    assert state["n"] >= 2, "thread died after the first raising callback"

def test_a_write_racing_start_is_still_seen(tmp_path, monkeypatch):
    """`start()` returning must mean "everything from now on is a change".

    The watcher thread can be descheduled between `Thread.start()` and its
    first statement. If the baseline snapshot is taken *there*, a write landing
    in that gap is absorbed into the baseline and never fires -- which on a
    loaded machine silently drops the first transcript written after `bridge
    serve` boots, and is what made
    `test_a_raising_callback_does_not_kill_the_thread` flaky on CI.

    The gap is forced here rather than waited for, so this fails every time
    against a thread-side baseline instead of once in a hundred runs.
    """
    import threading as _threading

    calls = []
    real_start = _threading.Thread.start

    def start_then_write(self):
        # Stand in for an arbitrarily long deschedule: the file appears after
        # `Thread.start()` is called but before the thread body can run.
        (tmp_path / "raced.jsonl").write_text("{}")
        real_start(self)

    monkeypatch.setattr(_threading.Thread, "start", start_then_write)
    w = FileWatcher(tmp_path, on_change=lambda: calls.append(1),
                    poll_s=0.02, quiet_s=0.02)
    w.start()
    monkeypatch.undo()
    try:
        time.sleep(0.4)
    finally:
        w.stop()
    assert calls, "a write racing start() was swallowed into the baseline"
