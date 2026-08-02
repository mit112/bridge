# Bridge — Scheduled Sessions & Custom Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compose an arbitrary prompt in the panel and **run it now** or **schedule it** to fire at
a chosen time; an in-process scheduler in `bridge serve` launches due jobs. Fills the 5h usage
window by sending sessions off at chosen moments.

**Architecture:** One new SQLite table `scheduled_runs`, all state transitions via single
conditional `UPDATE … WHERE status='pending'` statements (the linearization point — no
read-then-write, no `RETURNING`). A behavior-preserving `fire()` extraction from `POST /api/launch`
that both the route and the scheduler call. A daemon scheduler thread started in `__main__.py`'s
`serve` branch (not `create_app`), claiming one due job at a time to a `launching` state, launching,
then recording the terminal status. New `/api/schedule` routes and panel UI.

**Tech Stack:** Python 3.13, FastAPI/Jinja, sqlite3 (single shared connection under an `RLock`,
`store.transaction()` = `BEGIN…COMMIT`), pytest, `tools/falsify.py`.

**Spec:** `docs/superpowers/specs/2026-08-01-bridge-scheduled-sessions-design.md` (Codex-reviewed).
Read it; every task implements part of it.

## Global Constraints

- **At-most-once automatic launch, always fire late.** Claim one `pending` job at a time to
  `launching`; only after `launch_fn` returns does the row become `fired`/`failed`. A row stranded
  in `launching` by a crash is reconciled to `indeterminate` at startup and NEVER auto-retried.
- **Every state transition is one conditional statement inside `store.transaction()`** with a
  `rowcount` check. No `RETURNING` (SQLite ≥3.35 not guaranteed). No read-then-write.
- **`fire()` is a behavior-preserving extraction.** Existing `tests/test_api.py` launch tests MUST
  pass unchanged. The launcher (`launcher.launch`) still materializes the terminal prompt file and
  journals handoff consumption — `fire` only resolves the alias path, builds `LaunchSpec`, and calls
  `launch_fn`.
- **`LaunchResult` fields:** `launch_id: str`, `outcome: str` (`"started"`|`"failed"`),
  `session_id`, `short_id`, `error`, `note`. A normal spawn failure RETURNS `outcome="failed"`
  (`launcher._failed`); only pre-spawn validation RAISES `LaunchError`. Read the failure detail from
  `result.error`.
- **Scheduler lives in `__main__.py`'s `serve` branch, not `create_app`** — the `gc_prompt_files`
  precedent (tests build apps with `create_app`; a loop there would spawn real sessions).
- **Additive schema only:** append the `CREATE TABLE` to `store.SCHEMA`; never rebuild a table.
- **No spool journal for scheduled runs in v1** (durability decision in the spec) — SQLite only.
- **Time is epoch seconds server-side.** The browser converts local→epoch; no server timezone logic.
- Mutation discipline: commit before falsifying; validate anchors on the ordinary suite first;
  `/Users/mitsheth/.local/bin/uv run pytest -q` and
  `/Users/mitsheth/.local/bin/uv run python tools/falsify.py --spec …`.
- No AI attribution in any commit message.

## File Structure

- `src/bridge/store.py` — `scheduled_runs` in `SCHEMA`; a `ScheduledRun` row helper if useful;
  the store methods (create/list/get/claim_one_due/claim_specific/finish/edit_pending/
  cancel_pending/reconcile_launching).
- `src/bridge/models.py` — a `ScheduledRun` dataclass for `create_scheduled_run`'s input.
- `src/bridge/api.py` — extract `fire(...)`; the `/api/schedule` routes; a `ScheduleIn`/`SchedulePatch`
  Pydantic model.
- `src/bridge/scheduler.py` — **new** module: `run_scheduler(store, cfg, stop_event, launch_fn,
  interval)` loop + `tick(store, cfg, launch_fn, now)` (the unit tested in isolation).
- `src/bridge/__main__.py` — start the thread + `stop_event`/join/close in the `serve` branch;
  call `store.reconcile_launching(now)` at startup.
- `src/bridge/templates/dashboard.html`, `_card.html`, `base.html` (topbar) — UI.
- `src/bridge/static/app.css`, a new/existing JS file (`schedule.js`) — UI behavior.
- `tests/test_store.py`, `tests/test_scheduler.py` (new), `tests/test_api.py` — tests.
- `tools/mutations/scheduled-runs.json` — mutation spec.

Task order: **1 (store) → 2 (fire extraction) → 3 (schedule API) → 4 (scheduler) → 5 (UI) → 6
(mutations).** Tasks 3 and 4 both depend on 1+2; 5 depends on 3; 6 depends on all.

---

### Task 1: `scheduled_runs` store — table + conditional-transition methods

**Files:** `src/bridge/store.py` (SCHEMA + methods), `src/bridge/models.py` (`ScheduledRun`
dataclass), `tests/test_store.py`.

**Interfaces produced:**
- `models.ScheduledRun` — dataclass mirroring the insertable columns (id, project_path, prompt,
  summary, model, effort, mode, permission_mode, source_handoff_id, scheduled_for, created_at).
- `store.create_scheduled_run(job: ScheduledRun) -> str`
- `store.scheduled_runs(status: str | None = None) -> list[Row]` — active (`pending`,`launching`)
  first, then by `scheduled_for`.
- `store.get_scheduled_run(id) -> Row | None`
- `store.claim_one_due(now: int) -> Row | None`
- `store.claim_specific(id: str) -> Row | None`
- `store.finish_scheduled_run(id, *, status, launch_id=None, error=None, fired_at=None) -> None`
- `store.edit_pending(id, **fields) -> bool`
- `store.cancel_pending(id) -> bool`
- `store.reconcile_launching(now: int) -> int`

- [ ] **Step 1: Add the table to `SCHEMA`.** Append to the `SCHEMA` list (a `CREATE TABLE IF NOT
  EXISTS`), columns exactly per the spec's data-model table: `id TEXT PRIMARY KEY, project_path TEXT
  NOT NULL, prompt TEXT NOT NULL, summary TEXT, model TEXT, effort TEXT, mode TEXT NOT NULL,
  permission_mode TEXT, source_handoff_id TEXT, scheduled_for INTEGER NOT NULL, status TEXT NOT NULL
  DEFAULT 'pending', created_at INTEGER NOT NULL, claimed_at INTEGER, completed_at INTEGER, fired_at
  INTEGER, launch_id TEXT, error TEXT`. Add an index on `(status, scheduled_for)`.

- [ ] **Step 2: `ScheduledRun` dataclass** in `models.py` (defaults: optional fields `None`,
  `created_at` set by caller). Mirror `SessionRecord`/`Launch` style.

- [ ] **Step 3: Write the failing store tests** in `tests/test_store.py` (the `store` fixture):

```python
def _job(store, sid="j1", scheduled_for=1000, **kw):
    from bridge.models import ScheduledRun
    job = ScheduledRun(id=sid, project_path="/p", prompt="go", mode="background",
                       scheduled_for=scheduled_for, created_at=500, **kw)
    return store.create_scheduled_run(job)

def test_claim_one_due_claims_exactly_one_pending_job_at_or_before_now(store):
    _job(store, "a", scheduled_for=1000)
    _job(store, "b", scheduled_for=2000)          # future
    row = store.claim_one_due(now=1500)
    assert row["id"] == "a" and row["status"] == "launching" and row["claimed_at"] == 1500
    assert store.claim_one_due(now=1500) is None    # b is future; a already launching

def test_claim_one_due_is_a_single_winner_under_a_repeat_claim(store):
    _job(store, "a", scheduled_for=1000)
    assert store.claim_one_due(now=1500)["id"] == "a"
    assert store.claim_one_due(now=1500) is None     # already launching, not re-claimed

def test_finish_requires_a_prior_launching_and_records_terminal_fields(store):
    _job(store, "a", scheduled_for=1000)
    store.claim_one_due(now=1500)
    store.finish_scheduled_run("a", status="fired", launch_id="L1", fired_at=1600)
    r = store.get_scheduled_run("a")
    assert r["status"] == "fired" and r["launch_id"] == "L1" and r["fired_at"] == 1600
    assert r["completed_at"] is not None

def test_edit_and_cancel_only_touch_pending(store):
    _job(store, "a", scheduled_for=1000)
    assert store.edit_pending("a", prompt="new", scheduled_for=1200) is True
    assert store.get_scheduled_run("a")["prompt"] == "new"
    store.claim_one_due(now=1500)
    assert store.edit_pending("a", prompt="later") is False   # launching, immutable
    assert store.cancel_pending("a") is False

def test_cancel_pending_marks_cancelled(store):
    _job(store, "a", scheduled_for=1000)
    assert store.cancel_pending("a") is True
    assert store.get_scheduled_run("a")["status"] == "cancelled"
    assert store.claim_one_due(now=1500) is None              # cancelled never claimed

def test_reconcile_launching_flips_strays_to_indeterminate(store):
    _job(store, "a", scheduled_for=1000)
    store.claim_one_due(now=1500)                             # leaves it 'launching'
    assert store.reconcile_launching(now=9000) == 1
    assert store.get_scheduled_run("a")["status"] == "indeterminate"

def test_claim_specific_claims_a_pending_job_by_id(store):
    _job(store, "a", scheduled_for=9999)                      # far future
    assert store.claim_specific("a")["status"] == "launching"
    assert store.claim_specific("a") is None                 # not pending anymore
```

- [ ] **Step 4: Run them, verify they fail** (`AttributeError: … create_scheduled_run`).
  `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_store.py -k "claim or finish or edit or cancel or reconcile"`

- [ ] **Step 5: Implement the methods.** Each transition is one conditional statement inside
  `with self.transaction():` with a `rowcount` check. Reference implementations:

```python
def claim_one_due(self, now: int) -> sqlite3.Row | None:
    with self.transaction():
        row = self.conn.execute(
            "SELECT * FROM scheduled_runs WHERE status='pending' AND scheduled_for<=? "
            "ORDER BY scheduled_for, created_at, id LIMIT 1", (now,)).fetchone()
        if row is None:
            return None
        cur = self.conn.execute(
            "UPDATE scheduled_runs SET status='launching', claimed_at=? "
            "WHERE id=? AND status='pending'", (now, row["id"]))
        if cur.rowcount != 1:
            return None                      # lost the race
        return self.get_scheduled_run(row["id"])

def claim_specific(self, id: str) -> sqlite3.Row | None:
    with self.transaction():
        cur = self.conn.execute(
            "UPDATE scheduled_runs SET status='launching', claimed_at=? "
            "WHERE id=? AND status='pending'", (now_epoch(), id))
        return self.get_scheduled_run(id) if cur.rowcount == 1 else None

def finish_scheduled_run(self, id, *, status, launch_id=None, error=None, fired_at=None):
    with self.transaction():
        self.conn.execute(
            "UPDATE scheduled_runs SET status=?, launch_id=?, error=?, fired_at=?, "
            "completed_at=? WHERE id=? AND status='launching'",
            (status, launch_id, error, fired_at, now_epoch(), id))

def edit_pending(self, id, **fields) -> bool:
    cols = ", ".join(f"{k}=?" for k in fields)
    with self.transaction():
        cur = self.conn.execute(
            f"UPDATE scheduled_runs SET {cols} WHERE id=? AND status='pending'",
            (*fields.values(), id))
        return cur.rowcount == 1

def cancel_pending(self, id) -> bool:
    with self.transaction():
        cur = self.conn.execute(
            "UPDATE scheduled_runs SET status='cancelled', completed_at=? "
            "WHERE id=? AND status='pending'", (now_epoch(), id))
        return cur.rowcount == 1

def reconcile_launching(self, now: int) -> int:
    with self.transaction():
        cur = self.conn.execute(
            "UPDATE scheduled_runs SET status='indeterminate', completed_at=? "
            "WHERE status='launching'", (now,))
        return cur.rowcount
```
`edit_pending`'s `{cols}` interpolates only hardcoded caller keys, never user input (like the
existing `COLUMN_MIGRATIONS` interpolation note). `create_scheduled_run` and `scheduled_runs`/
`get_scheduled_run` follow the existing insert/select style; `scheduled_runs` orders
`CASE WHEN status IN ('pending','launching') THEN 0 ELSE 1 END, scheduled_for`.

- [ ] **Step 6: Run tests green, then full suite.** Commit: `git commit -m "Add the scheduled_runs
  table and its conditional-transition store methods"`.

---

### Task 2: Extract `fire()` from `POST /api/launch` (behavior-preserving)

**Files:** `src/bridge/api.py`, `tests/test_api.py`.

**Interface produced:**
`api.fire(store, cfg, *, project_path, prompt, mode, model, effort, permission_mode, title,
handoff_id, launch_fn) -> launcher.LaunchResult` — resolves
`effective_path = store.alias_map().get(project_path, project_path)`, builds the `LaunchSpec`
(exactly as the route builds it today), and calls `launch_fn`. It does NOT touch prompt files or
journaling (those are inside `launcher.launch`).

- [ ] **Step 1: Read the current `post_launch` route** (`src/bridge/api.py`, the `/api/launch`
  handler). Identify the block that resolves the path, constructs `LaunchSpec`, and calls
  `launch_fn` — that block is what moves into `fire`. The prompt/handoff *selection*, the 404/422
  handling, the default-title computation, and the response envelope STAY in the route.

- [ ] **Step 2: Write a failing focused test** asserting `fire` exists and that a scheduled-style
  call routes the alias-resolved path and the chosen mode into `launch_fn`:

```python
def test_fire_resolves_alias_and_passes_the_snapshot_to_launch_fn(client):
    c, store, _ = client
    store.set_alias("/old/path", "/Users/mitsheth/dev/demo")
    calls = []
    def fake_launch(store, cfg, spec, **kw):
        calls.append(spec)
        from bridge.launcher import LaunchResult
        return LaunchResult("L1", "started")
    from bridge import api
    res = api.fire(store, c.app.state... )   # see note
```
> Note: `fire` takes `store, cfg` directly; the test builds them from the `client` fixture's `store`
> and a `load({...})` cfg, and passes `launch_fn=fake_launch`. Assert `calls[0].project_path ==
> "/Users/mitsheth/dev/demo"` (alias resolved) and `calls[0].mode`/`.permission_mode` match. Keep it
> a pure unit call on `api.fire`, no HTTP.

- [ ] **Step 3: Run it, verify it fails** (`AttributeError: module 'bridge.api' has no attribute
  'fire'`).

- [ ] **Step 4: Implement `fire`** by moving the spec-construction+launch tail of `post_launch`
  into it (add `effective_path = store.alias_map().get(project_path, project_path)` and use it for
  `LaunchSpec.project_path`). Rewrite `post_launch` to compute its selections/validations, then
  `result = fire(store, cfg, project_path=…, prompt=selected_prompt, mode=body.mode, …,
  handoff_id=chosen_handoff_id, launch_fn=launch_fn)`, then map `LaunchError → 422` around the call
  and build the same envelope from `result`.

- [ ] **Step 5: Run the FULL suite** — the existing `/api/launch` tests are the real regression
  gate and MUST pass unchanged. `/Users/mitsheth/.local/bin/uv run pytest -q`. Then the focused
  `fire` test. Commit: `git commit -m "Extract a behavior-preserving fire() from POST /api/launch"`.

---

### Task 3: `/api/schedule` routes

**Files:** `src/bridge/api.py` (routes + `ScheduleIn`/`SchedulePatch` models), `tests/test_api.py`.
**Depends on:** Task 1 (store), Task 2 (`fire`, for run-now).

- [ ] **Step 1: Write failing route tests** in `tests/test_api.py`:

```python
def test_schedule_create_list_and_cancel(client):
    c, store, _ = client
    r = c.post("/api/schedule", json={"project_path": "/Users/mitsheth/dev/demo",
        "prompt": "do it", "scheduled_for": 1000, "mode": "background"})
    assert r.status_code == 201
    jid = r.json()["id"]
    assert any(j["id"] == jid for j in c.get("/api/schedule").json())
    assert c.delete(f"/api/schedule/{jid}").status_code == 200
    assert store.get_scheduled_run(jid)["status"] == "cancelled"

def test_schedule_edit_is_pending_only(client):
    c, store, _ = client
    jid = c.post("/api/schedule", json={"project_path": "/Users/mitsheth/dev/demo",
        "prompt": "x", "scheduled_for": 1000, "mode": "background"}).json()["id"]
    assert c.patch(f"/api/schedule/{jid}", json={"prompt": "y"}).status_code == 200
    store.claim_one_due(now=2000)                      # now 'launching'
    assert c.patch(f"/api/schedule/{jid}", json={"prompt": "z"}).status_code == 409

def test_run_now_claims_and_fires_via_fire(launch_app):
    # launch_app injects a launch_fn double (no real spawn); see existing launch tests
    c, store, _, launch_fn = launch_app
    jid = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "go",
        "scheduled_for": 9_000_000_000, "mode": "background"}).json()["id"]
    r = c.post(f"/api/schedule/{jid}/run-now")
    assert r.status_code == 200
    assert store.get_scheduled_run(jid)["status"] in ("fired", "failed")
    assert c.post(f"/api/schedule/{jid}/run-now").status_code == 409   # not pending anymore

def test_schedule_rejects_bad_mode_and_prompt(client):
    c, _, _ = client
    assert c.post("/api/schedule", json={"project_path": DEMO, "prompt": "x",
        "scheduled_for": 1000, "mode": "nope"}).status_code == 422
    assert c.post("/api/schedule", json={"project_path": DEMO, "prompt": "a\x00b",
        "scheduled_for": 1000, "mode": "background"}).status_code == 422
```

- [ ] **Step 2: Run them, verify they fail** (404 on the routes).

- [ ] **Step 3: Implement `ScheduleIn`/`SchedulePatch`** mirroring `LaunchIn`'s validators
  (`mode` from `launcher.MODES`, `permission_mode` from `launcher.PERMISSION_MODES`), and validate
  `prompt` with `launcher.validate_prompt` (map its error to 422).

- [ ] **Step 4: Implement the routes** in `create_app`:
  - `POST /api/schedule`: validate, `project_path = store.alias_map().get(path, path)`,
    `store.create_scheduled_run(ScheduledRun(id=str(uuid4()), created_at=now_epoch(), …))`, 201.
  - `GET /api/schedule`: `[dict(r) for r in store.scheduled_runs()]`.
  - `PATCH /api/schedule/{id}`: re-validate prompt if present; `ok = store.edit_pending(id,
    **patch_fields)`; `ok` → 200 with `dict(store.get_scheduled_run(id))`; else 404 if
    `get_scheduled_run(id) is None` else 409.
  - `DELETE /api/schedule/{id}`: `ok = store.cancel_pending(id)`; 200 / (404 if unknown else 409).
  - `POST /api/schedule/{id}/run-now`: `row = store.claim_specific(id)`; `None` → 404 if unknown
    else 409; else call the shared `_fire_claimed_job(store, cfg, row, launch_fn)` helper (Step 5)
    and return the resulting status.

- [ ] **Step 5: Extract `_fire_claimed_job(store, cfg, row, launch_fn)`** — the shared tail used by
  BOTH run-now and the scheduler (Task 4): call `fire(...)` with the row's snapshot and
  `handoff_id=row["source_handoff_id"]`; on `LaunchError` → `finish_scheduled_run(status="failed",
  error=…)`; on `result.outcome=="started"` → `finish(status="fired", launch_id=result.launch_id,
  fired_at=now_epoch())`; else `finish(status="failed", launch_id=result.launch_id,
  error=result.error)`. Returns the final row. Put it at module scope so Task 4 imports it.

- [ ] **Step 6: Green + full suite. Commit** `git commit -m "Add the /api/schedule routes and the
  shared claimed-job firing helper"`.

---

### Task 4: The scheduler thread

**Files:** `src/bridge/scheduler.py` (new), `src/bridge/__main__.py`, `tests/test_scheduler.py`
(new). **Depends on:** Task 1, Task 2, Task 3 (`_fire_claimed_job`).

**Interface produced:**
- `scheduler.tick(store, cfg, launch_fn, now) -> int` — claims and fires all due jobs, returns the
  count fired; the unit under test.
- `scheduler.run_scheduler(store, cfg, stop_event, launch_fn, interval)` — the loop.

- [ ] **Step 1: Write failing scheduler tests** (`tests/test_scheduler.py`) with an injected clock
  and a `launch_fn` double — no real spawn:

```python
def test_tick_fires_due_jobs_and_records_launch_id(store, cfg):
    _job(store, "a", scheduled_for=1000)             # helper like Task 1's
    def fake(store, cfg, spec, **kw): 
        from bridge.launcher import LaunchResult; return LaunchResult("L1", "started")
    from bridge import scheduler
    assert scheduler.tick(store, cfg, fake, now=1500) == 1
    assert store.get_scheduled_run("a")["status"] == "fired"
    assert store.get_scheduled_run("a")["launch_id"] == "L1"

def test_tick_marks_a_returned_failure_failed_not_crashed(store, cfg):
    _job(store, "a", scheduled_for=1000)
    def fake(store, cfg, spec, **kw):
        from bridge.launcher import LaunchResult
        return LaunchResult("L1", "failed", error="boom")
    from bridge import scheduler
    scheduler.tick(store, cfg, fake, now=1500)
    r = store.get_scheduled_run("a")
    assert r["status"] == "failed" and r["error"] == "boom"

def test_tick_survives_one_raising_job_and_still_fires_the_next(store, cfg):
    _job(store, "a", scheduled_for=1000); _job(store, "b", scheduled_for=1001)
    calls = {"n": 0}
    def fake(store, cfg, spec, **kw):
        from bridge.launcher import LaunchResult, LaunchError
        calls["n"] += 1
        if calls["n"] == 1: raise LaunchError("bad")   # pre-spawn raise
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
```
> `cfg` fixture: `load({"db_path": tmp_path/"s.db", "spool_dir": tmp_path/"sp", "launches_dir":
> tmp_path/"l"})`. Add it to `tests/test_scheduler.py` or reuse a conftest one.

- [ ] **Step 2: Run them, verify they fail** (no `bridge.scheduler`).

- [ ] **Step 3: Implement `scheduler.py`:**

```python
import logging, threading
from bridge.store import now_epoch
from bridge import launcher
from bridge.api import _fire_claimed_job     # the shared tail from Task 3

log = logging.getLogger(__name__)

def tick(store, cfg, launch_fn=launcher.launch, now=None) -> int:
    fired = 0
    while (row := store.claim_one_due(now if now is not None else now_epoch())) is not None:
        _fire_claimed_job(store, cfg, row, launch_fn)
        fired += 1
    return fired

def run_scheduler(store, cfg, stop_event, launch_fn=launcher.launch, interval=30):
    while not stop_event.wait(interval):
        try:
            tick(store, cfg, launch_fn)
        except Exception:                    # a bad tick never kills the daemon
            log.exception("scheduler tick failed")
```
`_fire_claimed_job` already catches per-job `LaunchError` and records terminal status, so one bad
job cannot stop the `while` loop (verify the Task-3 helper does not re-raise). If it can raise,
wrap the `_fire_claimed_job` call in `tick` in a per-job `try/except` that logs and continues.

- [ ] **Step 4: Wire into `__main__.py`'s `serve` branch.** Before `uvicorn.run(...)`:
  `store.reconcile_launching(now_epoch())` (log the count); create `stop = threading.Event()`;
  `t = threading.Thread(target=scheduler.run_scheduler, args=(store, cfg, stop), daemon=True);
  t.start()`. Wrap `uvicorn.run(...)` in `try/finally`; in `finally`: `stop.set(); t.join(timeout=…)`
  then close the store if the serve branch owns it. Keep it OUT of `create_app` (tests must not
  spawn the thread).

- [ ] **Step 5: Green + full suite. Commit** `git commit -m "Add the in-process scheduler thread
  and start it from the serve branch"`.

---

### Task 5: Panel UI — compose, schedule, and the Scheduled section

**Files:** `src/bridge/templates/_card.html`, `dashboard.html`, `base.html`;
`src/bridge/static/app.css`; `src/bridge/static/schedule.js` (new, or extend `launch.js`);
`tests/test_api.py` (render assertions). **Depends on:** Task 3. **Consult the `design-guardrails`
skill before writing markup** (data-dense dashboard rules; reuse existing form/section patterns).

- [ ] **Step 1: Failing render tests** — the dashboard shows a Scheduled section and a topbar count
  when a job exists; the section is present-but-collapsed when empty (mirror the hidden-`<details>`
  decision). Assert on the rendered `/` HTML with a seeded `scheduled_run`.

- [ ] **Step 2–4:** Build, minimally, reusing existing patterns:
  - A per-project **compose** control on `_card.html` (textarea + Run now/Schedule…), posting to
    `/api/launch` (run-now) or `/api/schedule` (with a `datetime-local` → epoch conversion in JS,
    plus a mode select). Mirror the existing launch controls and PATCH-on-`focusout` edit pattern.
  - A **"Schedule…"** affordance on a queued handoff that POSTs `/api/schedule` with
    `source_handoff_id`.
  - A global **Scheduled** `<section>` (always rendered, collapsed when empty) listing active jobs
    with local time, project, mode, and Edit/Cancel/Run-now; `fired`/`failed`/`indeterminate` shown
    briefly with a retry on failure. Feed it from `GET /api/schedule` (or server-render from
    `store.scheduled_runs()` in the dashboard handler — match how the dashboard renders other
    sections).
  - A **topbar** pending count beside queued-handoffs; a **5h hint** in the schedule form from the
    already-computed last-5h total.
  - `datetime-local` → epoch and epoch → local display in JS; server stays epoch-only.
  - Keyboard-operable, labeled inputs, WCAG AA contrast (design-guardrails).

- [ ] **Step 5: Green + full suite. Commit** `git commit -m "Add the compose box, schedule form,
  and Scheduled panel section"`.

---

### Task 6: Mutation coverage

**Files:** `tools/mutations/scheduled-runs.json`.

- [ ] **Step 1: Write the spec** with mutations that each named test catches (verify anchors with
  `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_mutation_specs.py` before falsifying):
  - `claim_one_due` bound `scheduled_for<=?` → `<?` (fails `test_claim_one_due_claims_exactly_one_…`).
  - claim guard `status='pending'` in `claim_one_due`'s UPDATE removed/loosened
    (fails `test_claim_one_due_is_a_single_winner_under_a_repeat_claim`).
  - `finish_scheduled_run`'s `WHERE … status='launching'` guard removed
    (fails `test_finish_requires_a_prior_launching_…`).
  - `cancel_pending`'s `status='pending'` guard removed (fails `test_edit_and_cancel_only_touch_pending`).
  - `reconcile_launching` `status='launching'` → something else (fails the reconcile test).
  - the scheduler's `outcome == "started"` branch inverted (fails
    `test_tick_marks_a_returned_failure_failed_not_crashed` or the fired test).
  - the run-now `claim_specific` guard (fails `test_run_now…` second call expecting 409).

- [ ] **Step 2: Validate anchors** (`pytest -q tests/test_mutation_specs.py`).
- [ ] **Step 3: Commit the spec**, then **Step 4: falsify** — expect all caught. A survivor: check
  vacuity/equivalence first.

---

## Decisions (locked — do not relitigate)

Same as the spec's "Locked decisions" + "Durability decision": at-most-once/`launching`/
`indeterminate`; conditional-statement transitions, no `RETURNING`; behavior-preserving `fire()`;
scheduler in `serve` not `create_app`; per-job mode; always fire late; epoch-only server time; no
spool journal for scheduled runs in v1 (documented consequence).

## Out of scope

No reusable prompt library; no recurring/cron repeats; no launchd backend; no auto-scheduling; no
spool journaling for scheduled runs. (All in the spec.)

## Self-Review

- **Spec coverage.** Data model + methods → Task 1. `fire()` extraction + alias/handoff flow →
  Task 2. `/api/schedule` incl. atomic run-now → Task 3. Scheduler loop + lifecycle + startup
  reconcile → Task 4. UI (compose/schedule/section/topbar/5h hint/handoff-schedule) → Task 5.
  Mutations → Task 6. Durability decision and time handling are documented constraints, not code.
- **Placeholder scan.** One deliberate prose note in Task 2 Step 2 (`c.app.state…`) is annotated as
  "build store+cfg from the fixture, call `api.fire` directly" — the implementer wires the exact
  handles; every other step carries real code.
- **Type consistency.** `LaunchResult(launch_id, outcome, session_id, short_id, error, note)` used
  in Tasks 2–4 (failure detail = `.error`). `_fire_claimed_job` defined in Task 3 Step 5, imported
  by Task 4 Step 3. `claim_one_due`/`claim_specific`/`finish_scheduled_run`/`edit_pending`/
  `cancel_pending`/`reconcile_launching` defined in Task 1, used in Tasks 3–4.
- **Ordering.** `fire` (Task 2) has no store dependency and could precede Task 1, but Task 1 first
  gives the riskiest schema change its own gate; Tasks 3–4 need both.
