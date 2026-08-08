# Instant (Push-Based) Liveness — Design

**Date:** 2026-08-07
**Status:** Design reviewed by two independent reviewers (opus + deepseek-v4-flash) and revised per their findings (see Independent review reconciliation). Awaiting user approval of the two baked-in assumptions (see Decisions) before implementation. NOT yet implemented.

## Problem

Liveness currently reaches the browser on a fixed ~3s SSE poll (`api.py`
`events()` does `time.sleep(3.0)` between builds). Even though agent-status
hook events arrive instantly at `POST /api/hooks` and are recorded, and the
active surface now reacts to the 3s live frames, nothing reaches the client in
under ~3s, and file-driven content (new sessions, transcript activity,
handoffs) waits on the ~15s reindex. The user wants **truly instant
(sub-second)** updates.

## Root cause

Two fixed delays, both server-side:
1. The SSE loop sleeps a flat 3s between builds — so even an already-recorded
   change waits up to 3s to be emitted.
2. Content changes (transcript files written by agent processes) are only
   observed by the ~15s periodic reindex.

## Approach

Turn the poll into **push**: an in-process notifier the SSE loop waits on, and
producers that wake it the instant something changes. A ~3s fallback timeout
stays, because some transitions have no event to push on.

### Core (required — nothing is instant without this)

**`ChangeNotifier` (`src/bridge/notify.py`, new).** A monotonic `revision:
int` guarded by a `threading.Condition`.
- `bump()` — under the Condition lock, increments `revision` then
  `notify_all()`. Must stay O(1) (increment + notify, no I/O): it is called
  from the event-loop thread by the `async` `/api/hooks` handler, so anything
  slower could stall the loop on lock contention during a many-waiter wake.
- `wait(since: int, timeout: float) -> int` — **the ordering is the whole
  correctness of the feature, so it is specified exactly:**
  ```
  with self._cond:
      if self._revision <= since:          # check UNDER the lock, before waiting
          self._cond.wait(timeout)         # while-loop not needed: one waiter,
      return self._revision                # any bump (revision>since) is the signal
  ```
  Because `bump()` increments `revision` under the same lock, a bump landing in
  the window between a waiter reading `since` and calling `wait` is not lost:
  the pre-wait check sees `revision > since` and returns immediately. Multiple
  SSE clients each wait independently; `notify_all` wakes them all.

**SSE loop change (`api.py` `events()`).** Replace `time.sleep(interval)` with
`revision = notifier.wait(since=revision, timeout=FALLBACK_S)`. **Capture the
baseline `since` BEFORE building the initial snapshot** (worst case is one
redundant, diff-gated rebuild — never a missed change). On return (event OR
timeout) it builds + diffs + emits exactly as today — the change-only
`live_signature` diff is unchanged, so a wake that turns out to be a no-op
emits nothing. `FALLBACK_S` stays ~3s so eventless transitions are no worse
than today.

**Per-connection rebuild floor (`REBUILD_FLOOR_S`, ~0.2s).** `live_signature`
gates the *wire*, not the *compute*: every wake runs `dashboard_builder.
live_patch()` → `agents.probe()` before the diff decides whether to emit. Under
push, a hook/transcript storm would wake every open connection per event and
multiply probe cost by event-rate × connections. So the loop coalesces: after
a build it will not rebuild again until `REBUILD_FLOOR_S` has elapsed — a bump
still wakes `wait`, but the loop sleeps out the remainder of the floor before
re-probing. This keeps sub-second feel while capping storm cost at ~1 build /
200ms per connection. This is the whole latency fix for anything a producer can
signal.

### Producers

**In-process events notify the hub directly (no file watching).** These are
code paths Bridge owns; watching the files they write would be indirection for
its own sake. Add a `notifier.bump()` call to: `POST /api/hooks` (agent status
— the highest-value signal), `/api/handoff` (edit/dismiss), `/api/launch`,
`/api/refresh`, and the scheduler when it fires a run.

**External file changes need a watcher.** Agent processes write transcript
`.jsonl` files under `cfg.claude_projects_dir` — Bridge does not control them.

`FileWatcher` (`src/bridge/watcher.py`, new): a background thread that detects
changes under `cfg.claude_projects_dir` and, **debounced**, calls
`refresh_coordinator.run_once()` (incremental reindex) — which bumps generation
and (via the coordinator, see below) the notifier. The backend is behind a
**single-method interface** (`start(callback)` / `stop()`, no config surface —
keep the seam minimal, not a speculative abstraction):
- **Default backend: mtime poll.** Stat the transcript tree every ~0.5s
  (`POLL_S`) via the indexer's existing `transcript_files()` walk; on any file
  mtime/size change since the last scan, schedule a reindex. Note: a dir-level
  stat cannot substitute — appending to an existing transcript (the *common*
  active-session case) does not bump the parent dir mtime — so it must stat
  files, i.e. cost is O(files) per tick. Zero dependencies, most portable
  (the launcher is already macOS-only, but the watcher stays pure stdlib),
  ~0.5s latency (sub-second).
- **Documented drop-in: `watchfiles`.** Native OS events (inotify/FSEvents),
  ~ms latency, one dependency. Swappable at the seam if truly-instant file
  latency is wanted later.

**Debounce = a real quiet period, not just the poll interval.** On detecting a
change, wait for a short quiet window (~200ms with no further change) before
calling `run_once()`, so a burst of appends coalesces into one reindex. (Poll-
interval-as-debounce only holds if the tick runs scan→run_once→sleep serially,
which also means a long reindex *stretches* the effective interval — acceptable,
never shrinks it.)

**Watcher/periodic contention.** `run_once()` acquires the coordinator's
existing `_run_lock`; the watcher-triggered reindex and the 15s periodic one
therefore serialize on it — no overlap, no index corruption. The watcher thread
simply blocks on the lock and runs when the periodic pass finishes. Mid-write
transcripts are safe by the indexer's inherited invariants (per-file offset
tracking, bad-line tolerance, shrink→rescan-from-0).

**Reindex → notifier (strict happen-after).** Give `RefreshCoordinator` an
optional `on_change` callback (defaulted `None`). It fires **after** the
`with self._status_lock:` block in `run_once` has published the new
`generation` — never before. If it fired first, the woken SSE loop would read
the *old* generation, take the `live_patch` branch instead of `full_update`,
and the new content would still wait for the ~3s fallback — silently defeating
the feature. `on_change` bumps the notifier; it covers both the periodic and
the watcher-triggered reindex.

### Lifecycle

**Ownership must match the existing wiring order.** In `__main__.py` the
`RefreshCoordinator` is constructed and its periodic thread started *before*
`create_app` is called. So the `ChangeNotifier` cannot originate inside
`create_app`. Construct it in `__main__.py` and inject it three ways: into
`RefreshCoordinator(on_change=notifier.bump)`, into the `FileWatcher` thread,
and into `create_app(...)` (which shares it with the producer routes). Keep
`on_change` optional/`None`-defaulted and `create_app`'s notifier param
optional so the route-test apps that build `create_app` directly — and
deliberately spawn no threads — still work: producers call `bump()` on a
notifier that simply has no waiters, and a missing notifier is a no-op.

The watcher thread and the existing periodic-reindex thread are started on serve
startup and stopped/joined on shutdown (same place `run_periodic` is wired
today). A watcher that fails to start or dies logs and is non-fatal: the ~3s
SSE fallback still catches every change, degrading to today's behavior.

**Threadpool occupancy (pre-existing, acknowledged).** `events()` is a sync
generator; Starlette holds one anyio threadpool worker (default cap 40) per SSE
connection for its life (up to `SSE_MAX_SECONDS=300`), exactly as the current
`time.sleep` loop does — swapping in `Condition.wait(timeout)` is not a
regression. But all sync routes share that pool, so many simultaneous SSE tabs
could starve other sync requests. Fine for a single-user local panel; for the
OSS case, add a simple open-SSE-connection cap (reject beyond N) and say so.

## Data flow

```
agent process writes transcript ─┐
                                 v
                         FileWatcher (mtime poll ~0.5s, debounced)
                                 │ run_once()
POST /api/hooks ─┐               v
/api/handoff  ──┤        RefreshCoordinator.run_once ── generation++ ──┐
/api/launch   ──┤                                                      │
/api/refresh  ──┤                                                      │
scheduler fire ─┘─────────────── notifier.bump() ─────────────────────┤
                                                                       v
                             ChangeNotifier.revision++ / notify_all
                                                                       │
                          SSE events(): wait(since, ~3s fallback) wakes
                                                                       │
                              build → live_signature diff → emit frame
                                                                       v
                          browser: live.js patch (Overview) / morph (rest)
```

## Independent review reconciliation (opus + deepseek-v4-flash, 2026-08-07)

Both reviewers judged the architecture sound; the changes below are folded in
above. Load-bearing corrections (both, unless noted):
- **`wait()` checks `revision > since` under the lock before blocking**, and the
  SSE loop captures `since` before the initial build. (Core §, lost-wakeup.)
- **`on_change` fires strictly after the generation is published** under
  `_status_lock` (opus, code-grounded) — else the wake reads the stale
  generation and misses `full_update`. (Producers §.)
- **Notifier constructed in `__main__`, injected three ways**, `on_change`/param
  optional (opus, matches real wiring order). (Lifecycle §.)
- **Per-connection rebuild floor (~200ms)** so a bump-storm doesn't multiply
  `agents.probe()` across connections. (Core §.)
- **Real quiet-period debounce** in the watcher, and **`_run_lock` serialization**
  for watcher-vs-periodic reindex. (Producers §.)
- Acknowledged-not-fixed (inherited or pre-existing): threadpool occupancy per
  SSE connection (unchanged from today; add a connection cap); mtime blind spot
  (indexer already skips equal size+mtime); pluggable seam kept to one method.

## Decisions (baked-in assumptions pending user confirmation)

1. **Watch mechanism = zero-dependency mtime poll (~0.5s), behind a pluggable
   backend.** Satisfies "sub-second", honors the project's stdlib-over-
   dependency default, and keeps the away-from-keyboard default conservative;
   `watchfiles` is a documented one-backend swap for ~ms latency.
2. **Git stays on the fallback.** Git dirty/commits refresh on the periodic
   reindex + cache (~3–15s), not sub-second. Watching every project working
   dir is noisy (node_modules/build output) and costly; a targeted
   `.git/HEAD`+index watch is a possible future extension, out of scope here.

## Error handling

- Watcher thread dies / fails to start: logged, non-fatal; ~3s SSE fallback
  covers everything (today's behavior).
- A bump that turns out to be a no-op (`live_signature` unchanged): emits
  nothing — the diff is the gate, unchanged from today.
- Multiple SSE clients (tabs): `notify_all` wakes all; each tracks its own
  `since` revision.
- Notifier `wait` must never hold the store lock across the wait (same
  discipline the current loop follows with `time.sleep`).

## Testing

- **`notify.py` (unit):** `bump()` wakes a blocked `wait`; `revision` is
  monotonic; `wait` returns on timeout with no bump; two concurrent waiters
  both wake on one `bump`. **Lost-wakeup as a pure predicate, not a timing
  race:** assert `wait(since)` with `revision > since` already true returns
  *immediately without blocking* (test the check-before-block property, not an
  unreproducible thread interleaving).
- **Ordering (integration):** a watcher/`run_once` reindex that bumps
  generation causes the SSE loop's next frame to be a `full_update` (new
  generation), never a stale `live_patch` — proves the happen-after.
- **Rebuild floor:** N bumps within `REBUILD_FLOOR_S` cause at most one
  rebuild/probe (proves storm coalescing).
- **`watcher.py` (unit):** creating/appending a file under a temp watch root
  triggers the reindex callback; a burst of writes within the quiet window
  coalesces to one call (proves the debounce, not just poll-coalescing);
  `stop()` joins the thread cleanly; a callback that raises does not kill the
  thread; the mtime scan uses a fast/injectable clock so tests don't sleep.
- **SSE (integration):** with a long fallback, a `notifier.bump()` mid-wait
  produces a frame promptly (proves push, not poll); with no bump, the loop
  still emits on the fallback timeout (proves the safety net).
- **Producers:** each of `/api/hooks`, `/api/handoff`, `/api/launch`,
  `/api/refresh`, and a scheduler fire advances the notifier's revision.
- Full existing suite stays green; no mutation-anchor drift (or re-sync with
  intent preserved if a pinned line moves).

## Out of scope

- `watchfiles`/native-event backend (documented seam only).
- Targeted git `.git` watching.
- Any change to the client (`live.js` / `liverefresh.js` / morph) — this is
  purely server-side latency; the client already reacts to frames as they
  arrive.
