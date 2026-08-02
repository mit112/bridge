# Bridge — Scheduled Sessions & Custom Prompts (design)

**Date:** 2026-08-01
**Status:** approved (brainstorm), pending implementation plan
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

## Locked decisions (from the 2026-08-01 brainstorm — do not relitigate)

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
- **Missed jobs: always fire late.** On any tick, every `pending` job with `scheduled_for <= now`
  fires. No grace window, no `missed` status. Accepted tradeoff: a job whose time passed while the
  panel was down or the laptop asleep starts the moment the scheduler next runs — possibly much
  later than intended. Chosen for simplicity; nothing is silently dropped.
- **Run-now reuses `POST /api/launch`.** `LaunchIn.prompt` is already optional and used when
  present, so a custom run-now is `POST /api/launch` with an explicit `prompt`. No new run path.

## Out of scope (deliberately)

- No reusable/named prompt library (follows from "one-off").
- No recurring / cron-style repeats — a job fires exactly once.
- No launchd/OS-level firing backend in this iteration (schema leaves the door open).
- No auto-scheduling to fill the window — time is always chosen manually. The form only *shows*
  the current 5h usage as a hint.

## Data model — `scheduled_runs`

One new table, additive (append to `store.SCHEMA` + `COLUMN_MIGRATIONS` conventions):

| column | type | notes |
|---|---|---|
| `id` | TEXT PK | a server-assigned uuid for the job itself. Distinct from any session id: a terminal launch's pre-assigned `session_id` is minted at fire time by the launcher and recorded via `launch_id`, not reused as the job id. |
| `project_path` | TEXT NOT NULL | resolved through the alias table at fire time, like a launch |
| `prompt` | TEXT NOT NULL | snapshot; a later handoff/compose edit never changes a live job |
| `summary` | TEXT | short title for the panel row; optional |
| `model` | TEXT | nullable — passed through to the launcher unvalidated, like a launch |
| `effort` | TEXT | nullable |
| `mode` | TEXT NOT NULL | `terminal` \| `background` (from `launcher.MODES`) |
| `permission_mode` | TEXT | nullable; validated against `launcher.PERMISSION_MODES` at the edge |
| `scheduled_for` | INTEGER NOT NULL | epoch seconds |
| `status` | TEXT NOT NULL DEFAULT 'pending' | `pending` \| `fired` \| `failed` \| `cancelled` |
| `created_at` | INTEGER NOT NULL | epoch |
| `fired_at` | INTEGER | epoch, set when fired |
| `launch_id` | TEXT | the `launches.id` the fire produced, when known |
| `error` | TEXT | failure detail when `status='failed'` |

Store methods (single-writer, `self._lock`, mirroring existing `store` style):
`create_scheduled_run(job) -> id`, `scheduled_runs(status=None) -> list[Row]` (pending first,
then by `scheduled_for`), `get_scheduled_run(id)`, `update_scheduled_run(id, **fields)` (pending
only — the route enforces), `claim_due_scheduled_runs(now) -> list[Row]` (atomic: `UPDATE … SET
status='fired', fired_at=? WHERE status='pending' AND scheduled_for<=? RETURNING *`, so two ticks
cannot double-fire), `set_scheduled_run_result(id, launch_id|error)`.

## Scheduler

A daemon thread started in **`__main__.py`'s `serve` branch, not `create_app`** — the exact
`gc_prompt_files` precedent: dozens of tests build apps directly with `create_app`, and a
side-effecting background loop there would spawn real sessions under test. The panel process is a
single process, so there is exactly one scheduler thread.

Loop: sleep ~30s, then `claim_due_scheduled_runs(now_epoch())` and fire each claimed job. Claiming
flips `pending → fired` inside the query, so a job is off the pending set before it launches and a
crash mid-launch leaves it `fired` with an `error` rather than re-firing on the next tick. Each
fire is wrapped: a raised launcher error sets `status` context to `failed` + `error` (via
`set_scheduled_run_result`) and the loop continues — one bad job never stops the scheduler or the
panel. Interval is a module constant; not user-configurable (YAGNI).

## Firing path — factor, don't duplicate

`POST /api/launch` today: resolves the project, materializes the prompt to a file, calls
`launch_fn`, journals consumption. Extract that core into one function
`fire(store, cfg, *, project_path, prompt, mode, model, effort, permission_mode, title, launch_fn)
-> LaunchResult` that both the route and the scheduler call. The scheduler passes the job's
snapshot fields and records the resulting `launch_id`. Terminal vs background behavior is entirely
the launcher's, unchanged: terminal pre-assigns a session id and `osascript`s a window; background
runs `claude --bg` and is linked back by short-id in `reindex` as today.

## API

- `POST /api/schedule` — body `{project_path, prompt, scheduled_for, mode, model?, effort?,
  permission_mode?, summary?}`. Validates `mode`/`permission_mode` at the edge from
  `launcher.MODES`/`PERMISSION_MODES` (one vocabulary source, like `LaunchIn`). Rejects a
  `scheduled_for` far in the past? No — "always fire late" means a past time is legal and fires on
  the next tick; the panel simply shows it as overdue. Returns the created job. 201.
- `GET /api/schedule` — list jobs, pending first then by `scheduled_for`; feeds the panel and the
  topbar count.
- `PATCH /api/schedule/{id}` — edit `prompt` / `scheduled_for` / launch params, **pending only**;
  404 unknown, 409 (or 422) if not pending. This is the "edit there" affordance.
- `DELETE /api/schedule/{id}` — cancel a pending job (sets `status='cancelled'`; a fired job is
  immutable). 404 unknown.
- Run-now custom prompt: existing `POST /api/launch` with an explicit `prompt`.

## UI

- **Compose box, per project** (on the card, beside the existing launch controls): a textarea →
  **Run now** (terminal/background as today) or **Schedule…** (reveals a datetime input + mode
  selector; POSTs `/api/schedule`).
- **"Schedule…" on an existing handoff** — snapshots the handoff's `next_prompt` into a new job.
- **Global "Scheduled" section** — rendered like the always-present `unattributed`/hidden
  sections: every pending job with its local time, project, mode, and Edit / Cancel / Run-now
  controls; recently `fired`/`failed` jobs shown briefly with their outcome and (on failure) a
  Run-now to retry.
- **Topbar** — a pending-scheduled count beside the queued-handoffs count.
- **5h-window hint** — the schedule form shows the current last-5h token total (already computed
  for the topbar) so a time can be chosen to fill the window. Read-only hint; no auto-scheduling.
- Consistent with the panel's standing UI decisions: forms reachable, controls keyboard-operable,
  no page reload on edit/cancel (the existing PATCH-on-`focusout` pattern), the section always
  rendered (collapsed when empty) so the first job has somewhere to land.

## Error handling

| Failure | Behavior |
|---|---|
| Launcher raises at fire time | Job → `failed` + `error`; visible with a Run-now retry; scheduler continues. |
| Two ticks race one job | Atomic claim (`UPDATE … WHERE status='pending'`) means only one wins. |
| Panel down / asleep past a job's time | Job fires on the next tick after serve returns (always fire late). |
| Cancelled job reaches a tick | Not `pending`, never claimed, never fires. |
| Edit/cancel of an already-fired job | Rejected (409/404); a fired job is immutable. |
| Scheduler thread itself errors | Per-job `try`/`except`; the loop and the panel survive one bad job. |

No scheduler failure may take down the panel — the same invariant every probe already honors.

## Testing

- **Store:** create/list/get/update/cancel; `claim_due_scheduled_runs` fires only `pending` jobs
  at/under `now`, flips status atomically, and is idempotent across two calls (second claims
  nothing).
- **Scheduler:** a tick with an injected clock and a `launch_fn` double (no real spawn — the
  discipline the existing launch tests already use) fires due jobs, records `launch_id`, marks a
  raising launch `failed`, and never fires a cancelled/future job.
- **API:** schedule/list/edit/cancel routes; pending-only edit enforcement; edge validation of
  `mode`/`permission_mode`; run-now-with-prompt through `/api/launch`.
- **Mutation spec** `scheduled-runs.json`: the due-query bound (`<=` vs `<`), the `pending`
  guard in the claim, the status-flip, the pending-only edit guard, and the scheduler's fire call.
- Suite stays green; mutations 1:1 with the new behaviors.

## Open implementation notes (for the plan, not new decisions)

- The `fire` refactor must not change any existing `/api/launch` behavior or test — it is a pure
  extraction.
- Datetime input in the panel is local-time in the browser; the client converts to epoch before
  POST, so the server only ever stores/compares epoch seconds (no timezone logic server-side).
- The scheduler thread needs the same `Store` instance the app uses; the store's `RLock` already
  makes cross-thread access safe (its documented design).
