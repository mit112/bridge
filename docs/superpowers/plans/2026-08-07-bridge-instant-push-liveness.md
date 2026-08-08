# Instant Push-Based Liveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the SSE loop's fixed 3s poll with event-driven push so agent-status and file-driven changes reach the browser sub-second, keeping a ~3s fallback for eventless transitions.

**Architecture:** An in-process `ChangeNotifier` (monotonic revision + `threading.Condition`). The SSE loop waits on it instead of sleeping. Producers `bump()` it: in-process API handlers directly, and a `FileWatcher` (mtime poll) → debounced reindex → coordinator `on_change` → bump. Constructed in `__main__` and injected into the coordinator, the watcher, and `create_app`. No client changes.

**Tech Stack:** Python 3.13 / FastAPI / Starlette sync-generator SSE, `threading` (stdlib only — no new dependency), pytest.

## Global Constraints

- **No new dependency.** Watcher uses stdlib mtime polling only (`watchfiles` is a documented future seam, not built).
- **`ChangeNotifier.bump()` must be O(1)** (increment + `notify_all` under the lock, no I/O) — it is called from the event-loop thread by the `async` `/api/hooks` handler.
- **`wait()` checks `revision > since` UNDER the Condition lock before blocking** — the lost-wakeup correctness depends on it.
- **Coordinator `on_change` fires STRICTLY AFTER the `_status_lock` block publishes the new generation** — never before, or the SSE wake reads the stale generation.
- **Notifier is constructed in `__main__`**, injected into `RefreshCoordinator(on_change=...)`, the `FileWatcher`, and `create_app(notifier=...)`; `on_change` and the `create_app` param are optional/`None`-defaulted so threadless route-test apps still work.
- **Never hold the store lock across `wait()`** (same discipline the current `time.sleep` loop follows).
- Surgical diffs; full existing suite stays green; re-sync any drifted mutation anchor preserving intent.
- Absolute coreutil paths in Bash (`/usr/bin/git`, etc.); tests via `.venv/bin/pytest`.

---

### Task 1: `ChangeNotifier` (`notify.py`)

**Files:**
- Create: `src/bridge/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces: `ChangeNotifier()` with `revision: int` (starts 0), `bump() -> int` (increments, notify_all, returns new revision), `wait(since: int, timeout: float) -> int` (returns current revision; if already `> since` returns immediately without blocking, else waits up to `timeout`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_notify.py
import threading, time
from bridge.notify import ChangeNotifier

def test_bump_increments_and_returns_monotonic_revisions():
    n = ChangeNotifier()
    assert n.revision == 0
    assert n.bump() == 1
    assert n.bump() == 2
    assert n.revision == 2

def test_wait_returns_immediately_when_revision_already_ahead():
    # The lost-wakeup property as a PURE predicate (no timing race): a bump that
    # already happened before wait() is never slept through.
    n = ChangeNotifier()
    n.bump()                     # revision 1
    start = time.monotonic()
    got = n.wait(since=0, timeout=5.0)
    assert got == 1
    assert time.monotonic() - start < 0.5, "wait blocked despite revision > since"

def test_wait_times_out_when_no_bump():
    n = ChangeNotifier()
    start = time.monotonic()
    got = n.wait(since=0, timeout=0.2)
    assert got == 0
    assert time.monotonic() - start >= 0.2

def test_a_bump_wakes_a_blocked_waiter():
    n = ChangeNotifier()
    woke = {}
    def waiter():
        woke["rev"] = n.wait(since=0, timeout=5.0)
    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    n.bump()
    t.join(timeout=2.0)
    assert woke["rev"] == 1

def test_two_waiters_both_wake_on_one_bump():
    n = ChangeNotifier()
    revs = []
    lock = threading.Lock()
    def waiter():
        r = n.wait(since=0, timeout=5.0)
        with lock:
            revs.append(r)
    ts = [threading.Thread(target=waiter) for _ in range(2)]
    for t in ts: t.start()
    time.sleep(0.05)
    n.bump()
    for t in ts: t.join(timeout=2.0)
    assert revs == [1, 1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_notify.py -v`
Expected: FAIL — `No module named 'bridge.notify'`.

- [ ] **Step 3: Implement `src/bridge/notify.py`**

```python
"""In-process change notifier: turns the SSE poll into push.

A monotonic revision guarded by a Condition. Producers `bump()` it; the SSE
loop `wait()`s on it instead of sleeping. The lost-wakeup guarantee lives in
`wait`: it compares `revision > since` UNDER the lock before blocking, so a
bump landing between a waiter reading `since` and calling `wait` is never slept
through.
"""

from __future__ import annotations

import threading


class ChangeNotifier:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._revision = 0

    @property
    def revision(self) -> int:
        with self._cond:
            return self._revision

    def bump(self) -> int:
        # O(1) on purpose: called from the event-loop thread by the async
        # /api/hooks handler, so it must never do I/O under the lock.
        with self._cond:
            self._revision += 1
            self._cond.notify_all()
            return self._revision

    def wait(self, since: int, timeout: float) -> int:
        with self._cond:
            if self._revision <= since:
                self._cond.wait(timeout)
            return self._revision
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_notify.py -v`
Expected: PASS (all five).

- [ ] **Step 5: Commit**

```bash
git add src/bridge/notify.py tests/test_notify.py
git commit -m "Add ChangeNotifier: revision + Condition with check-before-wait"
```

---

### Task 2: Coordinator `on_change` fired after status publish

**Files:**
- Modify: `src/bridge/refresh.py` (`RefreshCoordinator.__init__`, `run_once`)
- Test: `tests/test_refresh.py` (add; create if absent — check first with `ls tests/test_refresh.py`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `RefreshCoordinator(store, cfg, reindex_fn=..., interval_s=..., on_change: Callable[[], None] | None = None)`. `run_once` calls `on_change()` once, AFTER the `_status_lock` block, on BOTH the success and failure branches (a failed reindex still flips `server` to `unavailable`, which the SSE freshness reflects). A `None` callback is a no-op. An exception raised by `on_change` must not break `run_once`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_refresh.py (create the file with these imports if absent)
from bridge.refresh import RefreshCoordinator
from bridge.indexer import IndexStats

def _ok_reindex(store, cfg):
    return IndexStats()

def _boom_reindex(store, cfg):
    raise RuntimeError("boom")

def test_on_change_fires_after_generation_is_published(tmp_store_cfg):
    store, cfg = tmp_store_cfg
    seen = []
    coord = RefreshCoordinator(store, cfg, reindex_fn=_ok_reindex,
                               on_change=lambda: seen.append(coord.status_snapshot().generation))
    coord.run_once()
    # The callback observed the NEW generation, proving it ran after publish.
    assert seen == [1]

def test_on_change_fires_on_failure_too(tmp_store_cfg):
    store, cfg = tmp_store_cfg
    calls = []
    coord = RefreshCoordinator(store, cfg, reindex_fn=_boom_reindex,
                               on_change=lambda: calls.append(1))
    coord.run_once()
    assert calls == [1]

def test_on_change_none_is_a_noop(tmp_store_cfg):
    store, cfg = tmp_store_cfg
    coord = RefreshCoordinator(store, cfg, reindex_fn=_ok_reindex)
    coord.run_once()  # must not raise

def test_on_change_exception_does_not_break_run_once(tmp_store_cfg):
    store, cfg = tmp_store_cfg
    def raiser(): raise RuntimeError("cb boom")
    coord = RefreshCoordinator(store, cfg, reindex_fn=_ok_reindex, on_change=raiser)
    result = coord.run_once()
    assert result.completed is True  # run_once still succeeded
```

Note: reuse an existing store/cfg fixture from the current `tests/` suite (grep `tests/` for how `RefreshCoordinator` or `Store(` is built in a fixture, e.g. in `tests/test_store.py`/`conftest.py`); name the fixture `tmp_store_cfg` or adapt the tests to the existing fixture name. Do NOT invent a new store schema.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_refresh.py -v`
Expected: FAIL — `on_change` is an unexpected kwarg.

- [ ] **Step 3: Implement**

In `RefreshCoordinator.__init__`, add the parameter and store it:

```python
        interval_s: float = 15.0,
        on_change: "Callable[[], None] | None" = None,
    ) -> None:
        ...
        self.interval_s = interval_s
        self._on_change = on_change
```

Add a private helper and call it at the end of BOTH branches of `run_once` (after each `with self._status_lock:` block, before the `return`):

```python
    def _fire_on_change(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:  # noqa: BLE001 - a notifier bump must not kill refresh
            log.exception("refresh on_change callback failed")
```

In the failure branch, after the `log.exception(...)` line and before `return RefreshResult(False, ...)`, add `self._fire_on_change()`. In the success branch, after the `with self._status_lock:` block and before `return RefreshResult(True, ...)`, add `self._fire_on_change()`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_refresh.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bridge/refresh.py tests/test_refresh.py
git commit -m "Fire RefreshCoordinator on_change after the generation is published"
```

---

### Task 3: SSE loop waits on the notifier, with a rebuild floor + connection cap

**Files:**
- Modify: `src/bridge/api.py` (`create_app` signature; `events()` ~761-820)
- Test: `tests/test_api.py` (add SSE push tests near the existing `/events` tests — grep `def test` + `events` / `max_ticks`)

**Interfaces:**
- Consumes: `ChangeNotifier` (Task 1).
- Produces: `create_app(..., notifier: ChangeNotifier | None = None)`. When `None`, `create_app` constructs its own `ChangeNotifier()` so producer routes always have one. `events()` waits on `notifier.wait(since, FALLBACK_S)` instead of `time.sleep`, enforces `REBUILD_FLOOR_S` between builds, and rejects connections past `MAX_SSE_CONNECTIONS`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_api.py — reuse the existing app/client fixture used by the
# other /events tests (grep for how they call "/events" with max_ticks).
def test_events_emits_promptly_on_a_bump_without_waiting_the_fallback(app_with_notifier):
    # app_with_notifier: build create_app(store, cfg, notifier=n) with a LONG
    # fallback via the events() query param (?interval=... maps to FALLBACK_S)
    # and drive one tick; asserting a bump produces the second frame quickly.
    app, n = app_with_notifier
    # See the existing /events streaming-read helper; assert that after the
    # snapshot frame, calling n.bump() yields an update frame well under the
    # fallback timeout. Model this on the existing max_ticks-based test.
    ...

def test_events_still_emits_on_the_fallback_timeout_with_no_bump(app_with_notifier):
    # With no bump, the loop still wakes on FALLBACK_S and emits per today's
    # change-only rules. Assert the stream does not hang.
    ...
```

Note: the two SSE tests above are integration-shaped; write them by copying the existing `/events` test's streaming-read mechanism verbatim (it already reads frames with a capped `max_ticks`/`max_seconds`). Keep `FALLBACK_S` and `REBUILD_FLOOR_S` overridable via the existing `interval`/new query params so tests run fast and deterministically. If the existing test harness reads the stream synchronously, drive the bump from a helper thread. The precise assertion: a `bump()` after the initial snapshot produces the next frame in well under the fallback.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_api.py -k events -v`
Expected: FAIL (notifier param / prompt-emit behavior absent).

- [ ] **Step 3: Implement**

Add module constants near the other SSE constants (`SSE_MAX_SECONDS`):

```python
    REBUILD_FLOOR_S = 0.2      # min seconds between builds; caps probe cost under storms
    MAX_SSE_CONNECTIONS = 32   # sync SSE connections each pin a threadpool worker
```

In `create_app`, accept and store the notifier (mirroring the `refresh_coordinator` pattern):

```python
def create_app(
    store: Store, cfg: Config, launch_fn: LaunchFn = launcher.launch,
    refresh_coordinator: RefreshCoordinator | None = None,
    notifier: "ChangeNotifier | None" = None,
) -> FastAPI:
    ...
    if notifier is None:
        notifier = ChangeNotifier()
    app.state.notifier = notifier
```

(Import `from bridge.notify import ChangeNotifier` at the top.)

Rewrite the `events()` loop so it (a) captures `since` before the first build, (b) waits on the notifier instead of sleeping, (c) honors the rebuild floor, (d) caps connections. Replace the `time.sleep(interval)` tail and the loop scaffolding accordingly. The build/diff/emit body (`full_update`/`live_patch`, `live_signature`, `_frame`) is UNCHANGED — only the pacing changes:

```python
    _sse_connections = {"n": 0}
    _sse_lock = threading.Lock()

    @app.get("/events")
    def events(max_ticks: int | None = None, fallback: float = 3.0,
               floor: float = REBUILD_FLOOR_S, max_seconds: float = SSE_MAX_SECONDS):
        with _sse_lock:
            if _sse_connections["n"] >= MAX_SSE_CONNECTIONS:
                return JSONResponse({"detail": "too many live connections"},
                                    status_code=503)
            _sse_connections["n"] += 1

        def stream():
            started = time.monotonic()
            ticks = 0
            previous = None
            previous_generation = None
            previous_live_signature = None
            since = app.state.notifier.revision   # captured BEFORE the first build
            try:
                while True:
                    status = refresh_coordinator.status_snapshot()
                    built_at = time.monotonic()
                    if previous is None:
                        payload = dashboard_builder.full_update()
                        yield _frame("snapshot", payload)
                        previous_generation = payload["generation"]
                        previous = payload
                        previous_live_signature = live_signature(payload)
                    elif status.generation != previous_generation:
                        payload = dashboard_builder.full_update()
                        yield _frame("update", payload)
                        previous_generation = payload["generation"]
                        previous = payload
                        previous_live_signature = live_signature(payload)
                    else:
                        payload = dashboard_builder.live_patch()
                        current_live_signature = live_signature(payload)
                        if current_live_signature != previous_live_signature:
                            yield _frame("update", payload)
                        previous = payload
                        previous_live_signature = current_live_signature

                    ticks += 1
                    if max_ticks is not None and ticks >= max_ticks:
                        break
                    if time.monotonic() - started >= max_seconds:
                        yield _frame("refresh", {"reason": "stream capped"})
                        break

                    # Rebuild floor: never re-probe faster than `floor`. A bump
                    # wakes wait early, but we sleep out the remainder first.
                    elapsed = time.monotonic() - built_at
                    if elapsed < floor:
                        time.sleep(floor - elapsed)
                    # Wait for the next change (or the fallback). The lock is
                    # NOT held here.
                    since = app.state.notifier.wait(since=since, timeout=fallback)
                    yield ": heartbeat\n\n"
            finally:
                with _sse_lock:
                    _sse_connections["n"] -= 1

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
```

Note: the old signature had `interval: float = 3.0`. Rename to `fallback` as above; if any existing test calls `/events?interval=`, update those call sites to `?fallback=` in the same commit (grep `interval=` in `tests/`). Confirm `threading` is imported in `api.py` (add if not).

- [ ] **Step 4: Run the SSE + full api tests**

Run: `.venv/bin/pytest tests/test_api.py -k events -v` then `.venv/bin/pytest tests/test_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bridge/api.py tests/test_api.py
git commit -m "SSE loop waits on the notifier with a rebuild floor and connection cap"
```

---

### Task 4: Producer bumps on in-process events

**Files:**
- Modify: `src/bridge/api.py` (`post_hook`, `refresh`, the handoff POST/PATCH handlers, `launch`, and the schedule write endpoints `run_now`/`retry`/create/cancel)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `app.state.notifier` (Task 3).
- Produces: each listed handler calls `app.state.notifier.bump()` after it has recorded its effect (and before returning its response). Read-only handlers do NOT bump.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_api.py — reuse the app/client fixture that exposes the notifier.
def test_posting_a_hook_bumps_the_notifier(app_with_notifier_client):
    client, n = app_with_notifier_client
    before = n.revision
    client.post("/api/hooks", json={"hook_event_name": "Notification"})
    assert n.revision > before

def test_refresh_bumps_the_notifier(app_with_notifier_client):
    client, n = app_with_notifier_client
    before = n.revision
    client.post("/api/refresh")
    assert n.revision > before
```

(Add analogous assertions for `/api/launch`, `/api/handoff` POST + PATCH, and `/api/schedule/{id}/run-now` if a fixture that exercises them already exists in `test_api.py`; if a given endpoint has no easy existing test setup, cover it by reading the handler and confirming the `bump()` call is present rather than inventing a heavy fixture — note which in the report.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_api.py -k "bumps_the_notifier" -v`
Expected: FAIL (revision unchanged).

- [ ] **Step 3: Implement**

In each listed handler, after its state-changing work and before `return`, add `app.state.notifier.bump()`. For `post_hook` (async), the call is safe: `bump()` is O(1). For `refresh`, note it already calls `refresh_coordinator.run_once()` which (via Task 2 `on_change`) will ALSO bump — that double-bump is harmless (idempotent wake), but prefer relying on the coordinator's bump for `/api/refresh` and add the explicit `bump()` only to handlers that do NOT go through `run_once` (hooks, handoff, launch, schedule writes). Decide per-handler and note it; the invariant to satisfy is "every user write wakes the stream exactly once or more, never zero."

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_api.py -k "bumps_the_notifier" -v` then `.venv/bin/pytest tests/test_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bridge/api.py tests/test_api.py
git commit -m "Bump the change notifier on in-process user writes"
```

---

### Task 5: `FileWatcher` (`watcher.py`)

**Files:**
- Create: `src/bridge/watcher.py`
- Test: `tests/test_watcher.py`

**Interfaces:**
- Produces: `FileWatcher(root: Path, on_change: Callable[[], None], poll_s: float = 0.5, quiet_s: float = 0.2, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep)`. Methods `start()` (spawns a daemon thread), `stop()` (signals + joins). It stats every `*.jsonl` under `root` each `poll_s`; on a detected mtime/size change it waits for `quiet_s` of no further change, then calls `on_change()` once. `clock`/`sleep` are injectable so tests don't wait real time. A raising `on_change` is caught and logged; the thread survives.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_watcher.py
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_watcher.py -v`
Expected: FAIL — `No module named 'bridge.watcher'`.

- [ ] **Step 3: Implement `src/bridge/watcher.py`**

```python
"""Poll the transcripts dir for changes and fire a debounced callback.

Stdlib-only (no watchfiles). Stats every *.jsonl under `root` each `poll_s`;
appends to existing transcripts do NOT bump the parent dir mtime, so it must
stat files, not dirs. On a detected change it waits for a `quiet_s` lull before
firing `on_change` once, coalescing a burst of writes into one reindex.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class FileWatcher:
    def __init__(
        self, root: Path, on_change: Callable[[], None],
        poll_s: float = 0.5, quiet_s: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._root = Path(root)
        self._on_change = on_change
        self._poll_s = poll_s
        self._quiet_s = quiet_s
        self._clock = clock
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _snapshot(self) -> dict[str, tuple[float, int]]:
        out: dict[str, tuple[float, int]] = {}
        try:
            for child in self._root.iterdir():
                if not child.is_dir():
                    continue
                for f in child.glob("*.jsonl"):
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    out[str(f)] = (st.st_mtime, st.st_size)
        except OSError:
            pass
        return out

    def _run(self) -> None:
        last = self._snapshot()
        pending_since: float | None = None
        while not self._stop.wait(self._poll_s):
            current = self._snapshot()
            if current != last:
                last = current
                pending_since = self._clock()          # (re)start the quiet window
                continue
            if pending_since is not None and self._clock() - pending_since >= self._quiet_s:
                pending_since = None
                try:
                    self._on_change()
                except Exception:  # noqa: BLE001 - a bad reindex must not kill the watcher
                    log.exception("file watcher on_change failed")
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_watcher.py -v`
Expected: PASS. (If timing is flaky in CI, the injected `poll_s`/`quiet_s` are already small; do not add real long sleeps.)

- [ ] **Step 5: Commit**

```bash
git add src/bridge/watcher.py tests/test_watcher.py
git commit -m "Add a stdlib mtime-poll FileWatcher with a quiet-period debounce"
```

---

### Task 6: Wire the notifier + watcher into `serve` (`__main__.py`)

**Files:**
- Modify: `src/bridge/__main__.py` (the serve lifecycle around lines 148-195)
- Test: `tests/` — add a focused wiring test if a serve-lifecycle test exists; otherwise assert the chain in a small unit (construct notifier, coordinator with `on_change=notifier.bump`, call `run_once`, assert revision advanced).

**Interfaces:**
- Consumes: `ChangeNotifier` (Task 1), `RefreshCoordinator(on_change=...)` (Task 2), `create_app(notifier=...)` (Task 3), `FileWatcher` (Task 5).
- Produces: a fully wired serve path — notifier constructed once, injected three ways; watcher thread started/stopped.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_serve_wiring.py  (or add to an existing serve/lifecycle test module)
from bridge.notify import ChangeNotifier
from bridge.refresh import RefreshCoordinator
from bridge.indexer import IndexStats

def test_coordinator_on_change_bumps_the_injected_notifier(tmp_store_cfg):
    store, cfg = tmp_store_cfg
    n = ChangeNotifier()
    coord = RefreshCoordinator(store, cfg, reindex_fn=lambda s, c: IndexStats(),
                               on_change=n.bump)
    before = n.revision
    coord.run_once()
    assert n.revision > before, "a reindex must wake the notifier through on_change"
```

- [ ] **Step 2: Run to verify failure (or that it exercises the wiring)**

Run: `.venv/bin/pytest tests/test_serve_wiring.py -v`
Expected: PASS only once Task 1+2 exist (they do). This test guards the exact chain `__main__` builds; keep it.

- [ ] **Step 3: Wire `__main__.py`**

Replace the coordinator construction + `create_app` call so the notifier is built first and injected three ways, and start/stop the watcher alongside the refresh thread:

```python
    stop = threading.Event()
    notifier = ChangeNotifier()
    refresh_coordinator = RefreshCoordinator(store, cfg, on_change=notifier.bump)
    refresh_thread = threading.Thread(
        target=refresh_coordinator.run_periodic, args=(stop,), daemon=True
    )
    refresh_thread.start()
    watcher = FileWatcher(cfg.claude_projects_dir, on_change=refresh_coordinator.run_once)
    try:
        watcher.start()
    except Exception:  # noqa: BLE001 - the ~3s SSE fallback still covers changes
        log.exception("file watcher failed to start; falling back to periodic reindex")
    t = threading.Thread(
        target=scheduler.run_scheduler, args=(store, cfg, stop), daemon=True
    )
    t.start()

    try:
        uvicorn.run(
            create_app(store, cfg, refresh_coordinator=refresh_coordinator,
                       notifier=notifier),
            host="127.0.0.1", port=cfg.port,
        )
    finally:
        watcher.stop()
        _shutdown_scheduler(stop, t, store, refresh_thread)
```

Add imports at the top of `__main__.py`: `from bridge.notify import ChangeNotifier` and `from bridge.watcher import FileWatcher`. Confirm `log` exists in the module (it does — used by `_shutdown_scheduler`/prune); if not, add a module logger.

- [ ] **Step 4: Run the wiring test + the full suite**

Run: `.venv/bin/pytest tests/test_serve_wiring.py -v` then `.venv/bin/pytest -q`
Expected: PASS; report the count. If a mutation anchor drifted (a pinned line in `__main__.py`/`api.py`/`refresh.py` moved), STOP and report — re-sync preserving intent, then re-run `tools/falsify.py` on that spec.

- [ ] **Step 5: Commit**

```bash
git add src/bridge/__main__.py tests/test_serve_wiring.py
git commit -m "Wire the change notifier and file watcher into serve"
```

---

### Task 7: Verify (full suite + attended Arc live check)

**Files:** none (verification only). Defects fix in the owning task's file with a regression test.

- [ ] **Step 1: Full suite** — Run: `.venv/bin/pytest -q`. Expected: all pass; no mutation-anchor drift.

- [ ] **Step 2: Restart the local panel** on `:8787` (pre-authorized) so it serves the branch. Confirm `/` loads and `/events` still streams a snapshot frame (`curl --max-time 2 "http://127.0.0.1:8787/events?max_ticks=1"`).

- [ ] **Step 3: Arc — instant agent status.** Open a project page in Arc. Cause a hook to fire (start/stop a session, or trigger a needs-input). Confirm the status change appears in **well under a second**, not on a 3s beat.

- [ ] **Step 4: Arc — instant file-driven content.** With a session running, confirm transcript-driven changes (new session appearing, activity) reflect within ~0.5s. Make a change that only the reindex sees and confirm it is near-instant, not 15s.

- [ ] **Step 5: Arc — no regressions under the new pacing.** Confirm the freshness/connection strip still reads Live, drafts/scroll/focus still survive (the morph + live.js behavior is unchanged), and no visible flicker storm under rapid activity (the rebuild floor holds).

- [ ] **Step 6: Note results; fix any defect in its owning task with a regression test; re-run `.venv/bin/pytest -q`.** This step (live restart + observation) is the attended cutover — hold merge for the user.

---

## Self-Review

**Spec coverage:** notifier (T1); on_change-after-publish (T2); SSE wait + rebuild floor + connection cap + since-before-build (T3); producer bumps (T4); mtime-poll watcher + quiet-period debounce + injectable clock (T5); `__main__` ownership/injection + watcher lifecycle (T6); full-suite + attended Arc (T7). Git-on-fallback and the pluggable-seam-as-single-method are honored by omission (no git watch; watcher exposes only start/stop/on_change). ✓

**Placeholder scan:** the two SSE integration tests in T3 are described-not-coded because they must copy the existing `/events` streaming-read harness verbatim (inventing a second reader would drift from the real one); the brief points the implementer at that harness and states the exact assertion. Everything else is real code.

**Type consistency:** `ChangeNotifier.bump()/wait(since,timeout)/revision`, `RefreshCoordinator(..., on_change=)`, `create_app(..., notifier=)`, `FileWatcher(root,on_change,poll_s,quiet_s,clock,sleep).start()/stop()/is_alive()` — consistent across T1-T6.
