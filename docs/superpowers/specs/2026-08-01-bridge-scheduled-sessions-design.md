# Bridge — Scheduled Sessions & Custom Prompts (design)

**Date:** 2026-08-01
**Status:** approved (brainstorm), reviewed by Codex (gpt-5.6-sol) 2026-08-01, pending implementation plan
**Relates to:** the launcher (`launcher.py`, `POST /api/launch`), handoffs, and the
control-panel design at `docs/superpowers/specs/2026-07-31-bridge-control-panel-design.md`.

## Goal

Two capabilities, one coherent piece:

1. **Custom prompts** — compose an arbitrary prompt in the panel (not only a session-captured
   handoff) and either **run it now** or **schedule it**. One-off: consumed on launch, like a
   handoff; no reusable library.
2. **Scheduled sessions** — schedule a launch (a custom prompt, or an existing handoff's prompt)
   to fire at a chosen time. Motivation: fill the 5-hour rolling usage window by sending sessions
   off at chosen moments.

## Locked decisions (from the 2026-08-01 brainstorm + Codex review — do not relitigate)

- **Firing model: in-process now, launchd-ready later.** The scheduler is a background thread
  inside `bridge serve`. The job schema is firing-backend-agnostic (no launchd-specific columns),
  so an OS-level (launchd) backend can be added later with no data migration. Do NOT build the
  launchd abstraction until it is actually needed (YAGNI).
- **Custom prompt lifecycle: one-off, like handoffs.** A custom prompt is not a stored library
  item. It is text composed in the panel that feeds either an immediate launch or a scheduled
  job. The only new stored entity is the scheduled job itself.
- **Spawn mode: chosen per job.** The schedule form carries a `terminal | background` selector,
  defaulting to `background`. A terminal job `osascript`s a window open at fire time (works only
  when the GUI session is live); a background job runs `claude --bg`.
- **Delivery guarantee: at-most-once automatic launch, always fire late.** On any tick, every
  `pending` job with `scheduled_for <= now` is claimed and launched. No grace window. **This is
  at-most-once, not exactly-once** — a job whose fire outcome cannot be confirmed (process killed
  mid-spawn) is marked `indeterminate` and is NEVER auto-retried, because the external `claude`
  process may already be running; the user retries by hand. Accepted tradeoff: a job whose time
  passed while the panel was down/asleep starts the moment the scheduler next runs, possibly much
  later than intended. Chosen for simplicity; nothing is silently dropped.
- **Run-now reuses `POST /api/launch`.** `LaunchIn.prompt` is already optional and used when
  present, so a custom run-now from the compose box is `POST /api/launch` with an explicit
  `prompt`. Run-now on an *existing pending job* is a distinct atomic endpoint (see API).

## Out of scope (deliberately)

- No reusable/named prompt library (follows from "one-off").
- No recurring / cron-style repeats — a job fires exactly once.
- No launchd/OS-level firing backend in this iteration (schema leaves the door open).
- No auto-scheduling to fill the window — time is always chosen manually. The form only *shows*
  the current 5h usage as a hint.
- **No spool journal for scheduled runs in v1** (durability decision, below).

## Durability decision (Codex finding 7 — explicit, do not silently drop)

Handoffs are journaled to `~/.bridge/spool/` so a deleted/rebuilt `bridge.db` still recovers them
(`spool.py`; `store.py` calls handoffs "the only authored data … the only data a dropped database
genuinely loses"). Scheduled runs are ALSO authored data, so this is a real gap. **v1 stores
scheduled runs in SQLite only, with no spool journal.** Rationale: schedules are near-horizon and
the DB persists across restarts (it is never rebuilt in normal operation — only a manual `rm
bridge.db` loses it); a full append-only journal is not yet justified (YAGNI). **Consequence,
stated plainly:** a manual DB delete loses pending schedules while handoffs survive — the one place
scheduled runs are less durable than handoffs, by choice. If long-horizon scheduling ever lands,
add journaling following the `spool.py` pattern. This is a decision to accept, not an oversight.

## Data model — `scheduled_runs`

One new table, additive (append to `store.SCHEMA` + `COLUMN_MIGRATIONS` conventions):

| column | type | notes |
|---|---|---|
| `id` | TEXT PK | server-assigned uuid for the job. Distinct from any session id: a terminal launch's pre-assigned `session_id` is minted at fire time by the launcher and recorded via `launch_id`. |
| `project_path` | TEXT NOT NULL | the **canonical** path: `store.alias_map()`-resolved at *creation* time, so the firing path uses it verbatim as both `LaunchSpec.project_path` and the launch cwd (Codex finding 12 — `resolve_project` returns only a project id; the launcher uses the raw path for `cd`/`cwd`, so alias resolution must happen here, not be assumed downstream). |
| `prompt` | TEXT NOT NULL | creation-time snapshot; a later handoff/compose edit never changes a live job. Validated with `launcher.validate_prompt` at POST/PATCH (422 on NUL/oversize). |
| `summary` | TEXT | short title for the panel row; optional |
| `model` | TEXT | nullable — passed through to the launcher unvalidated, like a launch |
| `effort` | TEXT | nullable |
| `mode` | TEXT NOT NULL | `terminal` \| `background` (from `launcher.MODES`) |
| `permission_mode` | TEXT | nullable; validated against `launcher.PERMISSION_MODES` at the edge |
| `source_handoff_id` | TEXT | nullable, `REFERENCES handoffs(id)`. Set when the job was scheduled *from* a handoff; drives consumption on a started launch (see below). NULL for custom-prompt jobs. |
| `scheduled_for` | INTEGER NOT NULL | POSIX epoch seconds |
| `status` | TEXT NOT NULL DEFAULT 'pending' | `pending` → `launching` → (`fired` \| `failed` \| `indeterminate`), or `pending` → `cancelled` |
| `created_at` | INTEGER NOT NULL | epoch |
| `claimed_at` | INTEGER | epoch, set when a tick/run-now flips it to `launching` |
| `completed_at` | INTEGER | epoch, set on any terminal status |
| `fired_at` | INTEGER | epoch, set only on a `started` launch result |
| `launch_id` | TEXT | the `launches.id` the fire produced, when known |
| `error` | TEXT | failure detail when `status='failed'` |

**Status lifecycle (Codex finding 1):** `pending` is the only claimable state. A claim flips
`pending → launching` (never straight to `fired`). Only after the launcher **returns** does the row
reach a terminal state: `started` → `fired` (+ `fired_at`, `launch_id`); `failed` → `failed`
(+ `error`, `launch_id` if any). A row left in `launching` after a crash is reconciled at startup
to `indeterminate` and never auto-retried.

Store methods (single shared connection under `self._lock`, matching existing style). Each
state-transition method is **one conditional statement inside a `store.transaction()`
(`BEGIN … COMMIT`)** with a rowcount check, so the check-and-write is a single linearization point —
**no `RETURNING` dependency** (Codex finding 8: `RETURNING` needs SQLite ≥3.35, which Python 3.13
does not guarantee):
- `create_scheduled_run(job) -> id`.
- `scheduled_runs(status=None) -> list[Row]` — `pending`/`launching` first, then by `scheduled_for`.
- `get_scheduled_run(id)`.
- `claim_one_due(now) -> Row | None` — inside a transaction: `SELECT … WHERE status='pending' AND
  scheduled_for<=? ORDER BY scheduled_for, created_at, id LIMIT 1`; if found, `UPDATE … SET
  status='launching', claimed_at=? WHERE id=? AND status='pending'` and require `rowcount==1`
  (else another actor won — return None). Claims **one job per call**, not a batch, so a stall or
  crash strands at most one job.
- `claim_specific(id) -> Row | None` — same conditional `pending → launching` for a targeted
  run-now; returns the row on success, None (caller → 409) if not `pending`.
- `finish_scheduled_run(id, *, status, launch_id=None, error=None, fired_at=None)` — sets a
  terminal status + `completed_at`; asserts prior status was `launching`.
- `edit_pending(id, **fields) -> bool` — `UPDATE … WHERE id=? AND status='pending'`; `rowcount`
  is the linearization point (True win / False → 404-or-409 by the route).
- `cancel_pending(id) -> bool` — `UPDATE … SET status='cancelled', completed_at=? WHERE id=? AND
  status='pending'`; `rowcount` decides.
- `reconcile_launching(now) -> int` — at startup, `UPDATE … SET status='indeterminate',
  completed_at=? WHERE status='launching'`; returns the count for a startup log line.

## Scheduler

A daemon thread started in **`__main__.py`'s `serve` branch, not `create_app`** — the exact
`gc_prompt_files` precedent: dozens of tests build apps directly with `create_app`, and a
side-effecting background loop there would spawn real sessions under test. The panel is one process,
so there is exactly one scheduler thread.

**Startup:** call `reconcile_launching(now)` once (log any strays), then start the thread.

**Loop (Codex findings 1, 6, 9):**
```
while not stop_event.wait(INTERVAL):          # interruptible sleep, not time.sleep
    try:
        while (job := store.claim_one_due(now_epoch())) is not None:
            try:
                result = fire(store, cfg, **snapshot_of(job), launch_fn=launcher.launch)
            except LaunchError as e:            # pre-spawn validation only
                store.finish_scheduled_run(job.id, status="failed", error=str(e))
                continue
            if result.outcome == "started":
                store.finish_scheduled_run(job.id, status="fired",
                                           launch_id=result.launch_id, fired_at=now_epoch())
            else:                                # normal spawn failure RETURNS, not raises
                store.finish_scheduled_run(job.id, status="failed",
                                           launch_id=result.launch_id, error=result.detail)
    except Exception:                            # outer guard: a bad tick never kills the daemon
        log.exception("scheduler tick failed")   # loop continues on the next INTERVAL
```
Key corrections from the review: (a) claim **one job at a time** to `launching`, launch, *then*
record the terminal status — never mark `fired` before the launcher returns; (b) a normal spawn
failure **returns** `LaunchResult(outcome='failed')` (see `launcher._failed`), so inspect
`outcome`; only pre-row validation *raises* `LaunchError`; (c) the whole tick is wrapped so one bad
job (or a store error) logs and the loop survives. `INTERVAL` is a module constant (~30s), not
user-configurable (YAGNI).

**Thread lifecycle (Codex finding 9):** a `threading.Event` stop flag; the loop waits on
`stop_event.wait(INTERVAL)`. `serve` starts the thread immediately before `uvicorn.run(...)` and, in
a `finally`, sets the event, `join`s for a bounded timeout, then closes the `Store` only after the
thread has exited (so no launch is mid-flight against a closing connection). The thread stays a
daemon so a launch that outlives the join can never hang interpreter shutdown; such a case is logged
as an in-flight/indeterminate launch.

## Firing path — factor precisely, change nothing observable (Codex findings 4, 11)

**Correction to the earlier draft:** `POST /api/launch` does NOT materialize the prompt file or
journal consumption — the **launcher** does (`launcher.launch` writes `<session_id>.prompt` for
terminal mode, writes none for background, and journals handoff consumption). The route selects the
queued prompt/handoff, validates the handoff, computes the default title, maps `LaunchError → 422`,
and returns its eight-field envelope. The extraction must preserve all of that.

Extract only the **spec-construction + launch** core:
```
fire(store, cfg, *, project_path, prompt, mode, model, effort, permission_mode,
     title, handoff_id: str | None, launch_fn) -> LaunchResult
```
`fire` resolves `effective_path = store.alias_map().get(project_path, project_path)`, builds the
`LaunchSpec`, and calls `launch_fn`. It does NOT re-implement prompt-file creation or journaling —
those stay inside `launcher.launch`. `POST /api/launch` keeps its existing prompt/handoff selection,
404/422 handling, default-title logic, and response envelope, and now calls `fire` for the tail.
The scheduler calls `fire` with the job snapshot (its `project_path` is already canonical, so
`alias_map` is a no-op there) and passes `handoff_id=source_handoff_id`. This is a behavior-
preserving extraction: existing `/api/launch` tests must pass unchanged.

**Handoff consumption when scheduled (Codex finding 5):** a job carries `source_handoff_id`. On a
`started` result, `fire` (via the launcher's existing journal path, reached because `handoff_id`
now flows through) transitions that handoff `queued → consumed` and journals it; if the handoff is
already non-`queued`, leave it. A `failed`/`indeterminate` launch consumes nothing. The prompt fired
is always the job's creation-time snapshot, regardless of later handoff edits.

## API

Every mutation below is a single conditional store call (linearization point), never
read-then-write:
- `POST /api/schedule` — body `{project_path, prompt, scheduled_for, mode, model?, effort?,
  permission_mode?, summary?, source_handoff_id?}`. Validates `prompt` (`launcher.validate_prompt`
  → 422 on NUL/oversize), `mode`/`permission_mode` (from `launcher.MODES`/`PERMISSION_MODES`), and
  canonicalizes `project_path` via `alias_map`. A past `scheduled_for` is legal (fires next tick).
  201, returns the job.
- `GET /api/schedule` — list jobs, active (`pending`/`launching`) first then by `scheduled_for`;
  feeds the panel and the topbar count.
- `PATCH /api/schedule/{id}` — `edit_pending`; edits `prompt`/`scheduled_for`/launch params. Return
  = `edit_pending`'s rowcount: 1 → 200 with the row; 0 + row exists → 409 (already claimed/terminal);
  0 + no row → 404. Re-validates `prompt`.
- `DELETE /api/schedule/{id}` — `cancel_pending`; 200 on a canceled pending job, 409 if it already
  left `pending` (a `launching`/terminal job is immutable), 404 if unknown.
- `POST /api/schedule/{id}/run-now` (Codex finding 2) — atomic: `claim_specific(id)` (`pending →
  launching`); on success invoke `fire` on the same shared path and record the terminal status,
  returning it; on a lost claim return 409. This is the **only** correct "run a pending job now" —
  never `POST /api/launch` + `DELETE`, which races the scheduler into a double launch.
- Compose-box run-now (a job that was never scheduled): existing `POST /api/launch` with an explicit
  `prompt`, unchanged.

## UI

- **Compose box, per project** (on the card, beside the existing launch controls): a textarea →
  **Run now** (terminal/background as today) or **Schedule…** (reveals a datetime input + mode
  selector; POSTs `/api/schedule`).
- **"Schedule…" on an existing handoff** — snapshots the handoff's `next_prompt` into a job with
  `source_handoff_id` set.
- **Global "Scheduled" section** — rendered like the always-present `unattributed`/hidden sections:
  every active job with its local time, project, mode, and Edit / Cancel / Run-now controls;
  recently `fired`/`failed`/`indeterminate` jobs shown briefly with outcome and (on failure/
  indeterminate) a Run-now to retry.
- **Topbar** — a pending-scheduled count beside the queued-handoffs count.
- **5h-window hint** — the schedule form shows the current last-5h token total (already computed for
  the topbar) so a time can be chosen to fill the window. Read-only hint; no auto-scheduling.
- Consistent with the panel's standing UI decisions: forms reachable, controls keyboard-operable,
  no page reload on edit/cancel (the existing PATCH-on-`focusout` pattern), the section always
  rendered (collapsed when empty) so the first job has somewhere to land.

## Time handling (Codex finding 10)

`scheduled_for` is integer POSIX epoch seconds; the server only ever stores and compares epoch, no
timezone logic server-side. The browser owns civil-time conversion: it shows its resolved timezone/
offset next to the picker and converts the chosen local time to an absolute epoch before POST, so
DST gaps/folds are resolved at selection, not on the server. Restart, sleep/wake, forward wall-clock
jumps, and thread stalls all fire on the next tick (always fire late). A backward wall-clock jump
simply delays firing until the stored epoch is reached — and the `pending → launching` claim
guarantees a job never fires twice regardless of clock movement. (A full IANA gap/fold-rejecting
picker is optional hardening, not required for v1.)

## Error handling

| Failure | Behavior |
|---|---|
| Launcher **returns** `outcome='failed'` (normal spawn failure) | Job → `failed` + `error` (+ `launch_id` if any); Run-now retry; scheduler continues. |
| Launcher **raises** `LaunchError` (pre-spawn validation) | Job → `failed` + `error`; loop continues. |
| Process killed while a job is `launching` | Reconciled to `indeterminate` at next startup; **never auto-retried** (the spawn may have succeeded); manual Run-now offered. |
| Two ticks / a tick and a run-now race one job | The conditional `pending → launching` claim (rowcount==1) means exactly one wins. |
| Cancel/edit races the claim | Both are conditional on `status='pending'`; whichever commits first wins, the other gets 409. |
| Panel down / asleep past a job's time | Fires on the next tick after serve returns (always fire late). |
| Edit/cancel of a non-pending job | 409 (`launching`/terminal is immutable); 404 if unknown. |
| A tick itself errors (store/clock) | Outer `except` logs and the loop survives; the panel is never taken down. |

No scheduler failure may take down the panel — the invariant every probe already honors.

## Testing

- **Store:** create/list/get; `claim_one_due` claims exactly one due `pending` job to `launching`,
  is a no-op on future/non-pending rows, and two concurrent claims of the same row yield one winner;
  `edit_pending`/`cancel_pending` succeed only on `pending` (rowcount semantics); `finish_scheduled_run`
  requires a prior `launching`; `reconcile_launching` flips strays to `indeterminate`.
- **Scheduler:** a tick with an injected clock and a `launch_fn` double (no real spawn — the
  discipline the existing launch tests use) fires due jobs to `fired` with `launch_id`; a
  `LaunchResult(outcome='failed')` → `failed` (not a crash); a raised `LaunchError` → `failed`; a
  future/cancelled job is never claimed; an exception in one job does not stop the loop or skip
  later jobs.
- **Firing extraction:** existing `/api/launch` tests pass unchanged (behavior-preserving); a
  scheduled fire with `source_handoff_id` consumes the handoff on `started` and leaves it on failure.
- **API:** schedule/list/edit/cancel/run-now routes; the 200/409/404 matrix for edit/cancel/run-now
  against pending vs claimed vs terminal; prompt + mode + permission validation at the edge;
  compose-box run-now through `/api/launch`.
- **Mutation spec** `scheduled-runs.json`: the due bound (`<=` vs `<`), the `status='pending'` guard
  in every conditional claim/edit/cancel, the one-at-a-time claim (vs batch), the `outcome=='started'`
  branch, the `launching`-before-launch ordering, and the `reconcile_launching` startup call.
- Suite stays green; mutations 1:1 with the new behaviors.
