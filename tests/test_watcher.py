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
