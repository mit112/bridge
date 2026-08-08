# Instant (Push-Based) Liveness — Design

**Date:** 2026-08-07
**Status:** Design presented; awaiting user approval of two baked-in assumptions (see Decisions). NOT yet implemented.

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
- `bump()` — increments `revision`, `notify_all()`. Called by every producer.
- `wait(since: int, timeout: float) -> int` — blocks until `revision > since`
  or `timeout`, returns the current `revision`. Multiple SSE clients each wait
  independently; `notify_all` wakes them all.

**SSE loop change (`api.py` `events()`).** Replace `time.sleep(interval)` with
`revision = notifier.wait(since=revision, timeout=FALLBACK_S)`. On return
(event OR timeout) it builds + diffs + emits exactly as today — the change-only
`live_signature` diff is unchanged, so a wake that turns out to be a no-op
emits nothing. `FALLBACK_S` stays ~3s so eventless transitions are no worse
than today. This is the whole latency fix for anything a producer can signal.

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
and (via the coordinator, see below) the notifier. The detection backend is
**pluggable behind a small interface** so it can be swapped without touching
the wiring:
- **Default backend: mtime poll.** Stat the transcript tree every ~0.5s
  (`POLL_S`); on any file mtime/size change since the last scan, trigger a
  reindex. The poll interval IS the debounce (at most one reindex per 0.5s).
  Zero dependencies, robust cross-platform, ~0.5s latency (sub-second).
- **Documented drop-in: `watchfiles`.** Native OS events (inotify/FSEvents),
  ~ms latency, one dependency. Swappable at the backend seam if truly-instant
  file latency is wanted later.

**Reindex → notifier.** `RefreshCoordinator.run_once()` already increments
`generation` on a successful index. Give the coordinator an optional
`on_change` callback (or the notifier itself) so a completed reindex bumps the
notifier — this covers both the periodic reindex and the watcher-triggered one.

### Lifecycle

The notifier is created in `create_app`; the watcher thread and the existing
periodic-reindex thread are started on serve startup and stopped on shutdown
(same place `RefreshCoordinator.run_periodic` is wired today). A watcher that
fails to start or dies logs and is non-fatal: the ~3s SSE fallback still
catches every change, degrading to today's behavior.

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
  both wake on one `bump`; `wait(since=current)` returns promptly after a bump.
- **`watcher.py` (unit):** creating/appending a file under a temp watch root
  triggers the reindex callback; a burst within one poll interval coalesces to
  one call; `stop()` joins the thread cleanly; a callback that raises does not
  kill the thread.
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
