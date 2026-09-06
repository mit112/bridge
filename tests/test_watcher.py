import os
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
    """The quiet window is driven by the injected clock, not by wall time.

    Writing five times 0.03s apart against a 0.15s real-time debounce only
    coalesces if no scheduling hiccup ever stretches a gap past the window --
    thin enough to fail on a loaded machine for reasons that have nothing to do
    with the watcher. Freezing the clock makes the window unclosable no matter
    how long the burst actually takes; advancing it afterwards is what releases
    the single call.
    """
    calls = []
    fired = threading.Event()
    def cb():
        calls.append(1); fired.set()
    now = 0.0
    w = FileWatcher(tmp_path, on_change=cb, poll_s=0.02, quiet_s=0.15,
                    clock=lambda: now)
    w.start()
    try:
        f = tmp_path / "s.jsonl"
        for i in range(5):
            f.write_text("{}" * (i + 1))
            time.sleep(0.03)
        time.sleep(0.3)               # every write is now in the snapshot
        assert not calls, "the frozen clock must hold the quiet window open"
        now = 100.0                   # the window closes; the burst fires once
        assert fired.wait(2.0), "the coalesced burst never fired"
        time.sleep(0.2)               # a second call would land in here
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


def _cold_corpus(root: Path, dirs: int, per_dir: int) -> None:
    """A tree nothing has touched in an hour -- the idle-panel steady state."""
    old = time.time() - 3600
    made = [root]
    for d in range(dirs):
        sub = root / f"p{d}"
        sub.mkdir()
        made.append(sub)
        for i in range(per_dir):
            f = sub / f"s{i}.jsonl"
            f.write_text("{}\n")
            os.utime(f, (old, old))
    for d in made:
        os.utime(d, (old, old))


def test_polling_an_idle_corpus_does_not_stat_every_file(tmp_path, monkeypatch):
    """Per-poll work must be proportional to the change, not to the corpus.

    The naive snapshot stat()ed every *.jsonl on every poll: 9,202 files twice
    a second, measured at 11.9% CPU over 21h of a panel doing nothing. Here 300
    untouched files are polled ~20 times; statting them all would be ~6,000
    stat calls, so a budget well under one full pass proves the walk is gone.
    """
    _cold_corpus(tmp_path, dirs=6, per_dir=50)

    calls = {"n": 0}
    real_stat = os.stat

    def counting_stat(*a, **kw):
        calls["n"] += 1
        return real_stat(*a, **kw)

    w = FileWatcher(tmp_path, on_change=lambda: None, poll_s=0.02, quiet_s=0.02)
    monkeypatch.setattr(os, "stat", counting_stat)
    w.start()
    baseline = calls["n"]          # the one full walk start() is entitled to
    try:
        time.sleep(0.4)
    finally:
        w.stop()
    monkeypatch.undo()

    polled = calls["n"] - baseline
    assert polled < 300, f"polling cost {polled} stats over ~20 polls of 300 files"


def test_an_append_to_an_active_file_is_still_seen_within_a_second(tmp_path):
    """Appends do not bump the parent directory, so the hot set must catch them."""
    f = tmp_path / "live.jsonl"
    f.write_text("{}\n")
    ev = threading.Event()
    w = FileWatcher(tmp_path, on_change=ev.set, poll_s=0.02, quiet_s=0.02)
    w.start()
    try:
        with f.open("a") as fh:
            fh.write('{"more": 1}\n')
        assert ev.wait(1.0), "an append to an active transcript was missed"
    finally:
        w.stop()


def test_a_file_in_a_directory_created_after_start_is_seen(tmp_path):
    """A new project directory appears between polls; its parent's mtime moves."""
    ev = threading.Event()
    w = FileWatcher(tmp_path, on_change=ev.set, poll_s=0.02, quiet_s=0.02)
    w.start()
    try:
        nested = tmp_path / "new-project" / "deeper"
        nested.mkdir(parents=True)
        (nested / "s.jsonl").write_text("{}\n")
        assert ev.wait(1.0), "a transcript in a brand-new directory was missed"
    finally:
        w.stop()


def test_a_cold_change_is_still_reconciled_by_the_full_walk(tmp_path):
    """The cheap passes are an optimisation, not the source of truth.

    A file mutated in place with its directory's mtime left alone is invisible
    to both the directory pass and the hot set; the periodic full walk is what
    guarantees it is never lost, only late.
    """
    _cold_corpus(tmp_path, dirs=1, per_dir=1)
    target = tmp_path / "p0" / "s0.jsonl"
    ev = threading.Event()
    w = FileWatcher(
        tmp_path, on_change=ev.set, poll_s=0.02, quiet_s=0.02,
        hot_s=0.0, full_s=0.1,
    )
    w.start()
    try:
        old = time.time() - 3600
        target.write_text("{}\n{}\n")
        os.utime(target, (old, old))
        os.utime(tmp_path / "p0", (old, old))
        os.utime(tmp_path, (old, old))
        assert ev.wait(2.0), "the full walk never reconciled a cold change"
    finally:
        w.stop()


class _CountingEntry:
    """A `DirEntry` that reports its own `stat()` calls."""

    def __init__(self, entry, calls):
        self._entry = entry
        self._calls = calls

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def stat(self, *a, **kw):
        self._calls["n"] += 1
        return self._entry.stat(*a, **kw)


def test_a_warm_directory_is_not_relisted_on_every_poll_for_five_minutes(
    tmp_path, monkeypatch
):
    """The hot-directory guard exists for coarse mtimes, not for five minutes.

    A re-list is `scandir` plus one `stat` per entry. The real corpus has one
    directory holding 74% of its 11,392 transcripts, measured at 22.6 ms to
    re-list; under the old rule any activity in it bought that on every 0.5s
    poll for the next `hot_s` (300s) seconds. The directory's mtime moving is
    what triggers a re-list; recency past a tick or two adds nothing the
    periodic full walk does not already cover.
    """
    fat = tmp_path / "fat"
    fat.mkdir()
    # The files themselves are cold, so the file hot set is not what is being
    # measured here -- only the directory rule is.
    old = time.time() - 3600
    for i in range(40):
        f = fat / f"s{i}.jsonl"
        f.write_text("{}\n")
        os.utime(f, (old, old))
    # Touched 10s ago: inside the old 300s window, outside the new one.
    warm = time.time() - 10
    os.utime(fat, (warm, warm))
    os.utime(tmp_path, (warm, warm))

    w = FileWatcher(tmp_path, on_change=lambda: None)
    dirs, files = w._walk()

    calls = {"n": 0}
    real_stat = os.stat
    real_scandir = os.scandir

    def counting_stat(*a, **kw):
        calls["n"] += 1
        calls.setdefault("paths", []).append(a[0])
        return real_stat(*a, **kw)

    def counting_scandir(path):
        return [_CountingEntry(e, calls) for e in real_scandir(path)]

    monkeypatch.setattr(os, "stat", counting_stat)
    monkeypatch.setattr(os, "scandir", counting_scandir)
    w._poll(dirs, files)
    monkeypatch.undo()

    # One stat per known directory (the root, `fat`, and whatever the shared
    # fixtures put in `tmp_path`) and nothing else: not one of the 40 entries
    # of the warm directory should have been stat()ed.
    assert calls["n"] <= 8, f"cost {calls['n']} stats per poll: {calls['paths']}"
