# Scheduled-Run Spool Journal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `scheduled_runs` the same append-only disk journal handoffs already have, so
`rm ~/.bridge/bridge.db` followed by `bridge index` recovers pending schedules instead of silently
losing them.

**Architecture:** A new pure module `src/bridge/schedspool.py` mirrors `src/bridge/spool.py`, writing
one JSON file per creation and one per terminal status change into `~/.bridge/spool/schedules/`.
Replay is guarded on an empty `scheduled_runs` table and resolves each creation to `pending`, its
recorded terminal status, `missed`, or skipped-entirely. Journal calls live at API and boot call
sites, never inside `store.py`, so the store stays a pure database layer.

**Tech Stack:** Python 3.13, stdlib only (`json`, `os`, `tempfile`, `dataclasses`, `pathlib`),
SQLite via `sqlite3`, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-08-02-bridge-schedule-spool-design.md`

## Global Constraints

- Run tests with `/Users/mitsheth/.local/bin/uv run pytest -q`. Never bare `pytest`.
- Run mutations with `/Users/mitsheth/.local/bin/uv run python tools/falsify.py --spec tools/mutations/<file>.json`. Note `--spec`. It requires a committed clean tree.
- Use absolute coreutil paths in shell commands (`/usr/bin/git`, `/bin/ls`, `/usr/bin/grep`).
- `schedspool.py` is stdlib-only and imports no FastAPI, no Pydantic.
- **Do not edit `src/bridge/spool.py`.** `tools/mutations/task1-store-and-spool.json` anchors on occurrence counts in that file. `schedspool.py` imports `_atomic_write` and `_quarantine` from it; adding an alias or moving them would break `tests/test_mutation_specs.py`.
- Migrations are additive only: append to `SCHEMA`, never rebuild a table. This change adds **no** migration — `missed` is a value in the existing `status TEXT` column.
- No new `Config` field. The schedules directory derives from `cfg.spool_dir`.
- `pruned` is journal-only vocabulary and must never be written to the database `status` column.
- `missed` is produced by replay and by nothing else. No route, tick, or store transition creates it.
- No AI attribution in commit messages. Imperative mood, one logical change per commit.
- The baseline at the plan's start is **684 passing**. Per-task test counts below are indicative, not assertions — what matters is that the suite is green and the count only ever goes up.

---

### Task 1: The `schedspool` module — writers, loaders, and the test guard

**Files:**
- Create: `src/bridge/schedspool.py`
- Create: `tests/test_schedspool.py`
- Modify: `tests/conftest.py:72-79`

**Interfaces:**
- Consumes: `spool._atomic_write(payload: dict, stem: str, directory: Path) -> Path`, `spool._quarantine(path: Path, bad_dir: Path) -> None`, `models.ScheduledRun`
- Produces: `schedspool.journal(job: ScheduledRun, spool_dir: Path) -> Path`; `schedspool.journal_status(run_id: str, status: str, at: int, spool_dir: Path) -> Path`; `schedspool.STATUS_SUFFIX = ".status.json"`; `schedspool._dirs(spool_dir) -> tuple[Path, Path]` returning `(schedules_dir, bad_dir)`; `schedspool._Status(run_id, status, at)`; `schedspool._load(path) -> ScheduledRun`; `schedspool._load_status(path) -> _Status`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_schedspool.py`:

```python
"""The scheduled-run journal. Mirrors `test_spool.py`; see it for the shape."""

import json
from pathlib import Path

import pytest

from bridge import schedspool
from bridge.models import ScheduledRun
from bridge.store import Store

DEMO = "/Users/mitsheth/dev/demo"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "s.db")
    yield s
    s.close()


@pytest.fixture
def spool_dir(tmp_path):
    return tmp_path / "spool"


def job(jid="j1", **kw):
    fields = dict(
        id=jid,
        project_path=DEMO,
        prompt="do the thing",
        mode="background",
        scheduled_for=2_000_000_000,
        created_at=1_000_000_000,
    )
    fields.update(kw)
    return ScheduledRun(**fields)


def test_journal_writes_one_readable_record_named_for_the_job(spool_dir):
    path = schedspool.journal(job(), spool_dir)

    assert path == spool_dir / "schedules" / "j1.json"
    assert json.loads(path.read_text())["prompt"] == "do the thing"


def test_journal_writes_beside_the_handoff_journal_not_into_it(spool_dir):
    schedspool.journal(job(), spool_dir)

    # `spool.rebuild_if_empty` globs `drained/*.json` and parses each via
    # `spool._load` as a Handoff, so a
    # schedule record landing there would be quarantined as a corrupt handoff.
    assert not (spool_dir / "drained").exists()


def test_re_journalling_the_same_id_overwrites_rather_than_accumulates(spool_dir):
    schedspool.journal(job(), spool_dir)
    schedspool.journal(job(prompt="edited"), spool_dir)

    files = sorted((spool_dir / "schedules").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["prompt"] == "edited"


def test_journal_status_writes_a_distinguishable_record(spool_dir):
    path = schedspool.journal_status("j1", "fired", 1700, spool_dir)

    assert path.name == "j1.1700.status.json"
    assert path.name.endswith(schedspool.STATUS_SUFFIX)
    assert json.loads(path.read_text()) == {
        "run_id": "j1", "status": "fired", "at": 1700
    }


def test_a_creation_record_missing_a_required_field_will_not_load(spool_dir):
    path = schedspool.journal(job(), spool_dir)
    path.write_text(json.dumps({"id": "j1"}))

    with pytest.raises(Exception):
        schedspool._load(path)


def test_a_status_record_without_an_integer_at_will_not_load(spool_dir):
    path = schedspool.journal_status("j1", "fired", 1700, spool_dir)
    path.write_text(json.dumps({"run_id": "j1", "status": "fired", "at": "soon"}))

    with pytest.raises(Exception):
        schedspool._load_status(path)


def test_an_unknown_key_is_ignored_so_a_newer_bridge_still_replays(spool_dir):
    path = schedspool.journal(job(), spool_dir)
    data = json.loads(path.read_text())
    data["invented_later"] = True
    path.write_text(json.dumps(data))

    assert schedspool._load(path).id == "j1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_schedspool.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bridge.schedspool'`

- [ ] **Step 3: Write the module**

Create `src/bridge/schedspool.py`:

```python
"""The scheduled-run journal: append-only, and replay-only.

`spool.py` is both an outbox and a journal because `bridge handoff` runs while
the panel may be down. This module is a journal alone. A schedule can only be
authored *through* the panel (`POST /api/schedule`; there is no schedule CLI),
so there is no offline authoring path to catch and no outbox to drain.

Records live in `spool/schedules/`, deliberately not in `spool/drained/`.
`spool.rebuild_if_empty` globs `drained/*.json` and hands everything not ending in
`.status.json` to `spool._load`, which parses it as a `Handoff`, so a schedule
record in that directory would be quarantined as a corrupt handoff. A third record type in one directory would
compound the hazard the `STATUS_SUFFIX` comment in `spool.py` already flags.

Two vocabulary rules that the schema does not enforce and reviewers must:

- `pruned` is journal-only. Retention deletes a row; replay must skip it rather
  than insert it and mark it, which would put a row back that retention had
  already judged disposable. No database `status` column ever holds `pruned`.
- `missed` is the opposite: replay-only. It is what a creation with no terminal
  record and a `scheduled_for` in the past becomes, and nothing else produces
  it. A missed job never fires -- see `rebuild_if_empty`.

`test_a_pruned_job_does_not_come_back_and_a_past_due_one_is_missed` is the
regression test for both.
"""

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path

from bridge.models import ScheduledRun
from bridge.spool import _atomic_write, _quarantine

_FIELDS = frozenset(f.name for f in dataclasses.fields(ScheduledRun))

# Same discriminator role as `spool.STATUS_SUFFIX`: creations and status
# records share `schedules/`, so each loader must skip the other's files.
STATUS_SUFFIX = ".status.json"

# The only statuses `journal_status` writes, and the only ones `_load_status`
# accepts. Shape validation alone would let a malformed or future-written record
# claiming `pending` restore a fireable job, which is the one outcome replay must
# never produce. `missed` is absent on purpose: replay derives it, never records it.
JOURNALLED_STATUSES = frozenset(
    {"launching", "fired", "failed", "indeterminate", "cancelled", "pruned"}
)


@dataclass
class RebuildStats:
    restored: int = 0
    missed: int = 0
    skipped_pruned: int = 0
    bad: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class _Status:
    """One journalled status change. `at` is what orders the replay."""

    run_id: str
    status: str
    at: int


def _dirs(spool_dir: Path) -> tuple[Path, Path]:
    live = Path(spool_dir)
    return live / "schedules", live / "bad"


def journal(job: ScheduledRun, spool_dir: Path) -> Path:
    """Record a scheduled run the server accepted.

    Writing under `<id>.json` means an edit re-journals over its own record
    rather than accumulating, which is what keeps the journal's prompt text
    current -- the same property `PATCH /api/handoff/{id}` maintains.
    """
    schedules_dir, _ = _dirs(spool_dir)
    return _atomic_write(dataclasses.asdict(job), job.id, schedules_dir)


def journal_status(run_id: str, status: str, at: int, spool_dir: Path) -> Path:
    """Record a run's status change, so a rebuild replays where it ended up.

    Claims are journalled too, and that is not incidental. `claim_specific` has
    no `scheduled_for` guard, so `run-now` can fire a job whose scheduled time is
    still in the future. Without a `launching` record, a database lost in that
    window leaves only a creation record dated in the future, replay restores it
    as `pending`, and the scheduler fires the same job a second time. The record
    is what makes `rebuild_if_empty` answer `indeterminate` instead.

    The epoch is in seconds, inheriting `spool.journal_status`'s documented
    collision tradeoff: two statuses for one run inside one second write the same
    filename and the later wins. Still safe with `launching` in the vocabulary --
    it replays to `indeterminate`, so whichever of a colliding pair survives, the
    outcome is terminal and the job does not fire.
    """
    schedules_dir, _ = _dirs(spool_dir)
    return _atomic_write(
        {"run_id": run_id, "status": status, "at": at},
        f"{run_id}.{at}.status",
        schedules_dir,
    )


def _load(path: Path) -> ScheduledRun:
    """Parse one creation record, tolerating unknown keys but not missing ones.

    Unknown keys are ignored so a file written by a newer Bridge still replays.
    The four fields below are `ScheduledRun`'s required positional ones and the
    row cannot be reconstructed without them.

    `scheduled_for` and `created_at` are type-checked here rather than left to
    the caller. Replay compares `scheduled_for` against `now` *outside* the
    per-record insert handler, so a record carrying `"tomorrow"` would raise a
    `TypeError` that aborts the entire recovery instead of quarantining one file.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: top level is {type(data).__name__}, not object")
    job = ScheduledRun(**{k: v for k, v in data.items() if k in _FIELDS})
    if not job.id or not job.project_path or not job.prompt or not job.mode:
        raise ValueError(f"{path.name}: missing id, project_path, prompt or mode")
    if not isinstance(job.scheduled_for, int) or not isinstance(job.created_at, int):
        raise ValueError(f"{path.name}: scheduled_for and created_at must be ints")
    return job


def _load_status(path: Path) -> _Status:
    """Parse one status record, rejecting both bad shape and bad vocabulary.

    The vocabulary check is load-bearing, not defensive tidiness: a record
    claiming `pending` would restore a fireable job, so an unrecognised status
    goes to `bad/` rather than being applied or silently dropped.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: top level is {type(data).__name__}, not object")
    run_id, status, at = data.get("run_id"), data.get("status"), data.get("at")
    if not run_id or not status or not isinstance(at, int):
        raise ValueError(f"{path.name}: missing run_id, status or at")
    if status not in JOURNALLED_STATUSES:
        raise ValueError(f"{path.name}: status {status!r} is not a journalled status")
    return _Status(run_id, status, at)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_schedspool.py -q`
Expected: PASS, 7 tests

- [ ] **Step 5: Add the new writers to the real-directory guard**

This is the step most easily forgotten, and its own docstring says the omission
is invisible until a test has already written to the real `~/.bridge`.

In `tests/conftest.py`, add the `schedspool` import alongside the existing
`spool` import at the top of the file, then add this block immediately after the
existing `for name in ("write", "journal", ...)` loop that ends at line 79:

```python
    # Same contract for the scheduled-run journal. `rebuild_if_empty` is listed
    # even though it only reads: it takes a directory, and a test that replays
    # from the real spool is as wrong as one that writes to it.
    for name in ("journal", "journal_status", "rebuild_if_empty"):
        monkeypatch.setattr(
            schedspool, name,
            guarded("schedspool", name, getattr(schedspool, name), "spool_dir"),
        )
```

`rebuild_if_empty` does not exist until Task 2. Add it to the tuple now anyway
and let this step fail if it must — no: to keep the suite green between tasks,
list only `("journal", "journal_status")` here, and Task 2 Step 6 extends the
tuple. Use exactly `("journal", "journal_status")` in this task.

- [ ] **Step 6: Verify the guard actually guards**

Add to `tests/test_schedspool.py`:

```python
def test_the_guard_rejects_a_write_to_the_real_bridge_dir():
    """The autouse conftest guard must cover this module's writers.

    `RealBridgeDirTouched` derives from BaseException, so this cannot be caught
    with `pytest.raises(Exception)`.
    """
    from tests.conftest import RealBridgeDirTouched

    with pytest.raises(RealBridgeDirTouched):
        schedspool.journal(job(), Path.home() / ".bridge" / "spool")
```

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_schedspool.py -q`
Expected: PASS, 8 tests

- [ ] **Step 7: Run the full suite**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q`
Expected: PASS, 684 + 8 = 692 tests

- [ ] **Step 8: Commit**

```bash
/usr/bin/git add src/bridge/schedspool.py tests/test_schedspool.py tests/conftest.py
/usr/bin/git commit -m "Add a scheduled-run journal writer beside the handoff spool"
```

---

### Task 2: Replay — `rebuild_if_empty` and the `missed` status

**Files:**
- Modify: `src/bridge/schedspool.py` (append `rebuild_if_empty`)
- Modify: `src/bridge/models.py:146-152` (status vocabulary docstring)
- Modify: `tests/conftest.py` (extend the tuple from Task 1 Step 5)
- Test: `tests/test_schedspool.py`

**Interfaces:**
- Consumes: Task 1's `journal`, `journal_status`, `_load`, `_load_status`, `_dirs`, `RebuildStats`, `STATUS_SUFFIX`; `store.count_scheduled_runs() -> int`
- Also produces: `store.restore_scheduled_run(job: ScheduledRun) -> str` (added in Step 3b below, because replay is its only caller and Task 2 cannot run without it)
- Produces: `schedspool.rebuild_if_empty(store, spool_dir: Path, now: int) -> RebuildStats`

**`now` is a required parameter, deliberately.** `now_epoch` lives in `store.py`, not `models.py`,
and `schedspool` must not import from `store` — the store already has no filesystem dependency and
this module must not create the reverse one. `spool.py` solves the same problem the same way
(`journal_status` takes `at` from its caller), and `scheduler.tick` takes an explicit `now` too. The
single production caller in Task 5 passes `now_epoch()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_schedspool.py`:

```python
NOW = 1_500_000_000
PAST = NOW - 3600
FUTURE = NOW + 3600


def test_a_future_pending_job_replays_as_pending(store, spool_dir):
    schedspool.journal(job("j1", scheduled_for=FUTURE), spool_dir)

    stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert stats.restored == 1
    assert store.get_scheduled_run("j1")["status"] == "pending"


def test_a_fired_job_replays_as_fired_not_pending(store, spool_dir):
    schedspool.journal(job("j1", scheduled_for=PAST), spool_dir)
    schedspool.journal_status("j1", "fired", PAST + 1, spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("j1")["status"] == "fired"


def test_a_future_job_claimed_by_run_now_replays_as_indeterminate(store, spool_dir):
    """The duplicate-launch regression.

    `claim_specific` has no `scheduled_for` guard, so run-now can fire a job
    scheduled for tomorrow. If the database dies before the outcome is recorded,
    treating the creation record alone as `pending` would let the scheduler fire
    the same job again tomorrow. The claim record is what prevents that.
    """
    schedspool.journal(job("j1", scheduled_for=FUTURE), spool_dir)
    schedspool.journal_status("j1", "launching", NOW, spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("j1")["status"] == "indeterminate"


def test_a_replayed_terminal_row_is_reapable_by_retention(store, spool_dir):
    """`completed_at` NULL would make the row invisible to prune forever."""
    schedspool.journal(job("j1", scheduled_for=PAST), spool_dir)
    schedspool.journal_status("j1", "fired", PAST + 1, spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    # Assert the column, not a prune call: `prune_scheduled_runs`'s signature
    # changes in Task 3, and `completed_at` is the actual invariant.
    assert store.get_scheduled_run("j1")["completed_at"] == PAST + 1


def test_a_status_outside_the_vocabulary_is_quarantined_not_applied(store, spool_dir):
    """A record claiming `pending` would restore a fireable job."""
    schedspool.journal(job("j1", scheduled_for=PAST), spool_dir)
    forged = spool_dir / "schedules" / "j1.1700.status.json"
    forged.write_text(json.dumps({"run_id": "j1", "status": "pending", "at": 1700}))

    stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert stats.bad == 1
    assert store.get_scheduled_run("j1")["status"] == "missed"


def test_a_pruned_job_does_not_come_back_and_a_past_due_one_is_missed(
    store, spool_dir
):
    """The load-bearing test. All four replay rules in one scenario, against a
    database deleted the way `rm` deletes it."""
    schedspool.journal(job("future", scheduled_for=FUTURE), spool_dir)
    schedspool.journal(job("done", scheduled_for=PAST), spool_dir)
    schedspool.journal_status("done", "fired", PAST + 1, spool_dir)
    schedspool.journal(job("stale", scheduled_for=PAST), spool_dir)
    schedspool.journal(job("reaped", scheduled_for=PAST), spool_dir)
    schedspool.journal_status("reaped", "fired", PAST + 1, spool_dir)
    schedspool.journal_status("reaped", "pruned", PAST + 2, spool_dir)

    stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("future")["status"] == "pending"
    assert store.get_scheduled_run("done")["status"] == "fired"
    assert store.get_scheduled_run("stale")["status"] == "missed"
    assert store.get_scheduled_run("reaped") is None
    assert (stats.restored, stats.missed, stats.skipped_pruned) == (3, 1, 1)


def test_replay_is_skipped_when_the_table_is_not_empty(store, spool_dir):
    """Unguarded replay would resurrect finished jobs on every `bridge index`."""
    store.create_scheduled_run(job("already", scheduled_for=FUTURE))
    schedspool.journal(job("j1", scheduled_for=FUTURE), spool_dir)

    stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert stats.skipped == 1
    assert stats.restored == 0
    assert store.get_scheduled_run("j1") is None


def test_the_latest_status_wins_regardless_of_glob_order(store, spool_dir):
    schedspool.journal(job("j1", scheduled_for=PAST), spool_dir)
    schedspool.journal_status("j1", "failed", PAST + 9, spool_dir)
    schedspool.journal_status("j1", "fired", PAST + 1, spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("j1")["status"] == "failed"


def test_an_edited_prompt_is_what_replays(store, spool_dir):
    schedspool.journal(job("j1", scheduled_for=FUTURE), spool_dir)
    schedspool.journal(job("j1", scheduled_for=FUTURE, prompt="edited"), spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("j1")["prompt"] == "edited"


def test_a_corrupt_record_is_quarantined_and_the_replay_continues(store, spool_dir):
    schedspool.journal(job("good", scheduled_for=FUTURE), spool_dir)
    bad = spool_dir / "schedules" / "bad.json"
    bad.write_text("{not json")

    stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert stats.bad == 1
    assert store.get_scheduled_run("good")["status"] == "pending"
    assert (spool_dir / "bad" / "bad.json").exists()


def test_the_journal_survives_a_real_database_deletion(tmp_path, spool_dir):
    """`rm ~/.bridge/bridge.db` is the scenario this whole module exists for."""
    db = tmp_path / "db" / "s.db"
    store = Store(db)
    j = job("j1", scheduled_for=FUTURE)
    schedspool.journal(j, spool_dir)
    store.create_scheduled_run(j)
    store.close()

    db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    rebuilt = Store(db)
    try:
        stats = schedspool.rebuild_if_empty(rebuilt, spool_dir, now=NOW)
        assert stats.restored == 1
        assert rebuilt.get_scheduled_run("j1")["prompt"] == "do the thing"
    finally:
        rebuilt.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_schedspool.py -q`
Expected: FAIL — `AttributeError: module 'bridge.schedspool' has no attribute 'rebuild_if_empty'`

- [ ] **Step 3: Implement replay**

Append to `src/bridge/schedspool.py`:

```python
def rebuild_if_empty(store, spool_dir: Path, now: int) -> RebuildStats:
    """Replay the journal, but only into an empty `scheduled_runs` table.

    The guard is the whole point, exactly as in `spool.rebuild_if_empty`.
    Replaying onto a live table would resurrect finished runs on every
    `bridge index`.

    Retention emptying the table on its own does not make this unsafe: prune
    journals a `pruned` record per row it deletes, and rule 1 below skips those,
    so a table emptied by retention replays to an empty table rather than to
    every run Bridge has ever fired.

    Five rules per creation record, where the greatest `at` wins among a job's
    status records:

    1. A `pruned` record exists -> skip entirely. Retention already judged the
       row disposable; inserting and then marking it would put it back.
    2. The winner is terminal -> insert with that status.
    3. The winner is `launching` -> insert as `indeterminate`. A session may
       already have spawned and we cannot tell, which is exactly what
       `reconcile_launching` concludes for the in-database version of this.
    4. No status record, `scheduled_for` in the future -> `pending`. Still
       owed, and it fires normally.
    5. No status record, `scheduled_for` in the past -> `missed`.

    Rule 3 is what stops a duplicate launch. `run-now` can claim a job whose
    scheduled time is still ahead, so without it rule 4 would restore an
    already-launched job as `pending` and the scheduler would fire it again.

    Rule 5 is a deliberate refusal to fire retroactively. A job whose time passed
    while the database was gone would otherwise launch a session at an
    unpredictable moment for work the user may have forgotten scheduling -- the
    reasoning that made `bridge launch` refuse to spool. `missed` is terminal and
    visible; recovery is an explicit retry.
    """
    if store.count_scheduled_runs() > 0:
        return RebuildStats(skipped=1)

    when = now
    schedules_dir, bad_dir = _dirs(spool_dir)
    stats = RebuildStats()

    records: list[ScheduledRun] = []
    statuses: list[_Status] = []
    if schedules_dir.is_dir():
        for path in sorted(schedules_dir.glob("*.json")):
            if path.name.endswith(STATUS_SUFFIX):
                try:
                    statuses.append(_load_status(path))
                except Exception:  # noqa: BLE001 - one bad record cannot stop replay
                    _quarantine(path, bad_dir)
                    stats.bad += 1
                continue
            try:
                records.append(_load(path))
            except Exception:  # noqa: BLE001
                _quarantine(path, bad_dir)
                stats.bad += 1

    pruned = {s.run_id for s in statuses if s.status == "pruned"}
    # Latest wins. `pruned` is checked as a set above rather than by recency
    # because a prune is final regardless of what any later record claims.
    latest: dict[str, _Status] = {}
    for s in sorted(statuses, key=lambda s: (s.at, s.run_id)):
        latest[s.run_id] = s

    records.sort(key=lambda j: (j.created_at, j.id))
    for job in records:
        if job.id in pruned:
            stats.skipped_pruned += 1
            continue
        ended = latest.get(job.id)
        if ended is not None:
            # A claim with no outcome is `indeterminate`, never `pending`:
            # the launch may already have happened.
            job.status = (
                "indeterminate" if ended.status == "launching" else ended.status
            )
            job.completed_at = ended.at
        elif job.scheduled_for > when:
            job.status = "pending"
        else:
            job.status = "missed"
            job.completed_at = when
            stats.missed += 1
        try:
            # `restore_scheduled_run`, not `create_scheduled_run`: the latter
            # omits `completed_at`, and a terminal row without one can never
            # satisfy retention's `completed_at < ?` bound.
            store.restore_scheduled_run(job)
            stats.restored += 1
        except Exception:  # noqa: BLE001 - one failed insert cannot stop replay
            pass

    return stats
```

No new imports. `schedspool` still imports only `ScheduledRun` from `models` plus
the two helpers from `spool`. Do **not** add `from bridge.store import now_epoch`
— `now` is a parameter for exactly that reason.

- [ ] **Step 3b: Add the store method replay inserts through**

`create_scheduled_run` inserts twelve columns and not `completed_at`, `fired_at`,
`launch_id` or `error` — a newly authored schedule has none of them. Replay does,
and a restored `fired` row with `completed_at = NULL` can never satisfy
`prune_scheduled_runs`'s `completed_at < ?` bound, so it would sit in the table
forever.

Add to `src/bridge/store.py`, immediately after `create_scheduled_run`:

```python
    def restore_scheduled_run(self, job: ScheduledRun) -> str:
        """Insert a scheduled run with its terminal columns intact.

        Journal replay only. `create_scheduled_run` deliberately omits
        `completed_at`, `fired_at`, `launch_id` and `error` because a newly
        authored schedule has none of them; a recovered one can have all four,
        and a terminal row that arrives with `completed_at` NULL is invisible to
        retention forever. Kept separate rather than widening
        `create_scheduled_run` so that only recovery can write a terminal row
        directly.
        """
        with self._lock:
            self.conn.execute(
                "INSERT INTO scheduled_runs (id, project_path, prompt, summary, "
                "model, effort, mode, permission_mode, source_handoff_id, "
                "scheduled_for, status, created_at, claimed_at, completed_at, "
                "fired_at, launch_id, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.id, job.project_path, job.prompt, job.summary, job.model,
                    job.effort, job.mode, job.permission_mode, job.source_handoff_id,
                    job.scheduled_for, job.status, job.created_at or now_epoch(),
                    job.claimed_at, job.completed_at, job.fired_at, job.launch_id,
                    job.error,
                ),
            )
        return job.id
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_schedspool.py -q`
Expected: PASS, 16 tests

- [ ] **Step 4b: Add `retry_of` to `ScheduledRun` so replay can carry it**

The column exists in the database via `COLUMN_MIGRATIONS` (`store.py:177`) but not
on the dataclass, so `dataclasses.asdict` never journals it and replay would
restore every retry with `retry_of = NULL`. `retry_terminal`'s
`NOT EXISTS (SELECT 1 ... WHERE r.retry_of = orig.id)` guard would then permit a
second retry of an original that already has one — a duplicate launch.

In `src/bridge/models.py`, append one field to `ScheduledRun`, after `error`:

```python
    retry_of: str | None = None
```

Add `retry_of` to `restore_scheduled_run`'s column list and values tuple from
Step 3b, making it 18 columns and 18 placeholders:

```python
                "scheduled_for, status, created_at, claimed_at, completed_at, "
                "fired_at, launch_id, error, retry_of) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
```

with `job.retry_of` appended after `job.error` in the values.

`create_scheduled_run` stays at twelve columns — a newly authored schedule is
never a retry, and `retry_terminal` writes `retry_of` through its own
`INSERT ... SELECT`.

Add the regression test to `tests/test_schedspool.py`:

```python
def test_a_replayed_retry_keeps_its_provenance(store, spool_dir):
    """A NULL `retry_of` lets `retry_terminal` grant a second retry."""
    schedspool.journal(job("r1", scheduled_for=PAST, retry_of="orig"), spool_dir)
    schedspool.journal_status("r1", "fired", PAST + 1, spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("r1")["retry_of"] == "orig"
```

- [ ] **Step 5: Record `missed` in the status vocabulary**

In `src/bridge/models.py`, replace the `ScheduledRun` docstring body at lines
148-151 with:

```python
    """A session queued to launch at a future time.

    `status` moves pending -> launching -> {fired, failed, indeterminate,
    cancelled}; see `store.py`'s conditional-transition methods for the
    vocabulary. `missed` is the one status no transition produces: journal
    replay assigns it to a run whose `scheduled_for` passed while the database
    was gone, and a missed run never fires. `source_handoff_id` is None for a
    schedule authored directly rather than from a queued handoff.
    """
```

- [ ] **Step 6: Extend the conftest guard to cover replay**

In `tests/conftest.py`, change the `schedspool` tuple added in Task 1 from
`("journal", "journal_status")` to:

```python
    for name in ("journal", "journal_status", "rebuild_if_empty"):
```

- [ ] **Step 7: Run the full suite**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q`
Expected: PASS, 700 tests

- [ ] **Step 8: Commit**

```bash
/usr/bin/git add src/bridge/schedspool.py src/bridge/models.py tests/test_schedspool.py tests/conftest.py
/usr/bin/git commit -m "Replay the scheduled-run journal into an empty table"
```

---

### Task 3: Store changes — affected ids, retryable `missed`, and the stale comment

**Files:**
- Modify: `src/bridge/store.py:819-848` (`prune_scheduled_runs`, `reconcile_launching`)
- Modify: `src/bridge/store.py:813` (`retry_terminal`'s status guard)
- Modify: `src/bridge/store.py:396-397` (stale comment)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `store.prunable_scheduled_run_ids(before_epoch: int) -> list[str]`; `store.prune_scheduled_runs(ids: list[str]) -> int`; `store.launching_scheduled_run_ids() -> list[str]`; `store.reconcile_launching(now: int) -> int` (signature unchanged); `retry_terminal` accepting a `missed` row.

**Why read-only companions rather than mutate-then-return-ids.** Journaling from a return value
writes the record *after* the database changed. Boot swallows journal errors, so a failed write
would leave a row deleted with no `pruned` marker — and replay would resurrect it. The caller must
be able to journal first, which means it needs the ids *before* the mutation happens.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`, following the existing `store` fixture at lines
11-15 and the job-building style used by the tests at lines 226-240:

```python
def test_prunable_ids_names_the_rows_a_prune_would_delete(store):
    _terminal(store, "old-failed")
    store.conn.execute(
        "UPDATE scheduled_runs SET completed_at=100 WHERE id='old-failed'"
    )
    _job(store, "old-pending", scheduled_for=100)

    assert store.prunable_scheduled_run_ids(before_epoch=1000) == ["old-failed"]
    # Naming them must not delete them -- the caller journals in between.
    assert store.get_scheduled_run("old-failed") is not None


def test_prunable_ids_is_empty_when_nothing_is_old_enough(store):
    assert store.prunable_scheduled_run_ids(before_epoch=1000) == []


def test_prune_deletes_exactly_the_ids_it_is_given(store):
    _terminal(store, "a")
    _terminal(store, "b")

    assert store.prune_scheduled_runs(["a"]) == 1
    assert store.get_scheduled_run("a") is None
    assert store.get_scheduled_run("b") is not None


def test_prune_of_an_empty_list_touches_nothing(store):
    _terminal(store, "a")

    assert store.prune_scheduled_runs([]) == 0
    assert store.get_scheduled_run("a") is not None


def test_launching_ids_names_the_strays_reconcile_would_flip(store):
    _job(store, "a", scheduled_for=1000)
    store.claim_one_due(now=1500)

    assert store.launching_scheduled_run_ids() == ["a"]
    assert store.get_scheduled_run("a")["status"] == "launching"


def test_a_missed_run_can_be_retried(store):
    """Without this a replayed job is a dead end: retry rejects it and run-now
    requires `pending`, leaving the user to retype the schedule."""
    job = ScheduledRun(
        id="m1", project_path=DEMO, prompt="p", mode="background",
        scheduled_for=100, created_at=100, status="missed",
    )
    store.create_scheduled_run(job)

    row = store.retry_terminal("m1", new_id="m2")

    assert row is not None
    assert row["status"] == "launching"
    assert row["retry_of"] == "m1"
```

If `DEMO` and `ScheduledRun` are not already imported in `tests/test_store.py`,
check the top of the file and add whichever is missing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_store.py -q -k "prune_returns or reconcile_returns or missed_run_can"`
Expected: FAIL — prune/reconcile return `int`, and `retry_terminal` returns `None` for a `missed` row.

- [ ] **Step 3: Return affected ids**

Replace the body of `prune_scheduled_runs` in `src/bridge/store.py`, keeping the
existing docstring and appending the new paragraph:

```python
    def prune_scheduled_runs(self, ids: list[str]) -> int:
        """Delete exactly the runs named, returning how many went.

        The age and status bounds now live in `prunable_scheduled_run_ids`;
        this method trusts its argument. Only rows that are done should ever
        reach it: a `pending` job is still owed a launch no matter how long ago
        it was authored, and a `launching` one is either in flight or waiting
        for the next boot's `reconcile_launching`.

        The status clause is deliberately unfalsifiable, and there is no
        mutation for it. `completed_at` is written by exactly one place --
        finishing, cancelling or reconciling a row, all of which are terminal
        -- so over every reachable state a non-terminal row carries NULL, and
        `completed_at < ?` is NULL rather than true for it (SQL three-valued
        logic) and excludes it anyway. Either clause alone is therefore
        sufficient, so no test can tell them apart. This one stays because it
        states the intent the age bound only implies.

        Split from the id query above it so the caller can journal a `pruned`
        record per row *before* the row disappears. Journaling afterwards would
        mean a swallowed write leaves a deleted row with no marker, and replay
        would resurrect it.
        """
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self.transaction():
            cur = self.conn.execute(
                f"DELETE FROM scheduled_runs WHERE id IN ({marks})", tuple(ids))
            return cur.rowcount
```

Add the two read-only companions immediately above their mutating partners:

```python
    def prunable_scheduled_run_ids(self, before_epoch: int) -> list[str]:
        """Name the rows `prune_scheduled_runs` would delete, without deleting.

        The status and age clauses are the ones that used to live in the DELETE;
        see `prune_scheduled_runs` for why the status clause is unfalsifiable.
        """
        with self._lock:
            return [
                r["id"] for r in self.conn.execute(
                    "SELECT id FROM scheduled_runs "
                    "WHERE status NOT IN ('pending','launching') "
                    "  AND completed_at < ?", (before_epoch,))
            ]

    def launching_scheduled_run_ids(self) -> list[str]:
        """Name the strays `reconcile_launching` would flip, without flipping."""
        with self._lock:
            return [
                r["id"] for r in self.conn.execute(
                    "SELECT id FROM scheduled_runs WHERE status='launching'")
            ]
```

`reconcile_launching` gains an `ids` argument so it can flip only the rows the
caller managed to journal. Leaving an unjournalled row `launching` is the safe
failure: the next boot reconciles it again, whereas flipping it without a record
lets a run-now'd future job replay as `pending` and fire twice.

```python
    def reconcile_launching(self, now: int, ids: list[str]) -> int:
        """Flip the named stray `launching` rows to `indeterminate`.

        Takes explicit ids rather than flipping every `launching` row, so the
        caller can journal first and skip any row whose record would not write.
        """
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self.transaction():
            cur = self.conn.execute(
                f"UPDATE scheduled_runs SET status='indeterminate', completed_at=? "
                f"WHERE status='launching' AND id IN ({marks})", (now, *ids))
            return cur.rowcount
```

`test_reconcile_launching_flips_strays_to_indeterminate` at `tests/test_store.py:108`
calls `store.reconcile_launching(now=9000)` and must become
`store.reconcile_launching(9000, ["a"])`. It still asserts `== 1`.

- [ ] **Step 4: Make `missed` retryable**

In `retry_terminal`, change the status guard:

```python
                "WHERE orig.id=? AND orig.status IN ('failed','indeterminate','missed') "
```

And append to its docstring, after the existing final paragraph:

```
        `missed` is retryable for the same reason `failed` is: the run never
        launched. It reaches this method only from journal replay, and without
        it a recovered schedule could not be run at all -- `run-now` requires
        `pending`.
```

- [ ] **Step 5: Fix the now-false comment**

At `src/bridge/store.py:396-397` the current text is exactly:

```python
    # --- handoffs: the only authored data here, so the only data that a
    # --- dropped database genuinely loses. See spool.py for the journal.
```

Replace it, preserving the `# ---` section-marker prefix:

```python
    # --- handoffs: authored data, not derived from any transcript, so a
    # --- dropped database loses them. See spool.py for the journal.
    # --- `scheduled_runs` is the other authored table; see schedspool.py.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_store.py -q`
Expected: PASS

- [ ] **Step 7: Fix the caller and the existing tests the signature change breaks**

`reconcile_launching` is untouched. Only the prune caller changes, in
`src/bridge/__main__.py:109-113` — Task 5 adds the journaling around it:

```python
    reaped = store.prune_scheduled_runs(
        store.prunable_scheduled_run_ids(
            now_epoch() - SCHEDULED_RUN_RETENTION_DAYS * 86400
        )
    )
    if reaped:
        log.info("pruned %d finished scheduled run(s)", reaped)
```

Three existing tests call `prune_scheduled_runs(before_epoch=...)` and must be
updated to the two-call form. They are at `tests/test_store.py:226` and `:239`
(`test_prune_scheduled_runs_deletes_only_old_terminal_rows`,
`test_prune_scheduled_runs_reaps_cancelled_rows_too`), plus any hit from:

```bash
/usr/bin/grep -rn "prune_scheduled_runs" tests/
```

Each becomes, preserving the original assertion's intent:

```python
    ids = store.prunable_scheduled_run_ids(before_epoch=1000)
    assert store.prune_scheduled_runs(ids) == 1
```

`test_reconcile_launching_flips_strays_to_indeterminate` at
`tests/test_store.py:108` asserts `== 1` and stays correct — `reconcile_launching`
still returns a count. Do not change it.

- [ ] **Step 8: Run the full suite and repair mutation anchors**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q`

`tests/test_mutation_specs.py` will likely fail: `tools/mutations/scheduled-runs.json`
and `tools/mutations/scheduled-retry.json` anchor on `store.py` text this task
rewrote. For each reported offender, open the spec's `old` string and update it
to match the new source exactly, preserving the mutation's *intent* — do not
weaken a mutation into one the tests cannot catch.

Re-run until green: `/Users/mitsheth/.local/bin/uv run pytest -q`
Expected: PASS, 704 tests

- [ ] **Step 9: Commit**

```bash
/usr/bin/git add src/bridge/store.py src/bridge/__main__.py tests/test_store.py tools/mutations/
/usr/bin/git commit -m "Return affected ids from prune and reconcile, and allow retrying a missed run"
```

---

### Task 4: Journal at the API call sites

**Files:**
- Modify: `src/bridge/api.py:970-994` (`post_schedule`), `:1018-1027` (`patch_schedule`), `:1029-1033` (`delete_schedule`), `:1042-1065` (`retry_schedule`), `:327-384` (`_fire_claimed_job`)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `schedspool.journal(job, spool_dir)`, `schedspool.journal_status(run_id, status, at, spool_dir)`
- Produces: `POST /api/schedule` response gains a `journaled: bool` key.

- [ ] **Step 1: Write the failing tests**

**Fixture shapes — verified, do not guess.** Both fixtures yield tuples, and the
snippets below are written as if they did not. Unpack before use:

- `client` yields `(TestClient, store, pid)` (`tests/test_api.py:18-33`). Its
  config is `{"db_path": tmp_path/"a.db", "spool_dir": tmp_path/"spool"}`, so
  journal files land under `tmp_path/"spool"/"schedules"`.
- `launch_app` yields `(TestClient, store, cfg, fake)` (`tests/test_api.py:571-589`),
  where `fake` is `recording_launcher()`. Read that helper for the attribute
  holding recorded launches — `recorder.calls` below is a placeholder.

So each test below opens with an unpacking line, e.g.:

```python
def test_creating_a_schedule_journals_it(client, tmp_path):
    c, store, _pid = client
    r = c.post("/api/schedule", json={...})
```

Add to `tests/test_api.py`, in the scheduling section near line 1918, adapting
each snippet to that shape:

```python
def test_creating_a_schedule_journals_it(client, tmp_path):
    r = client.post("/api/schedule", json={
        "project_path": DEMO, "prompt": "p", "mode": "background",
        "scheduled_for": 2_000_000_000,
    })

    assert r.status_code == 201
    assert r.json()["journaled"] is True
    sid = r.json()["id"]
    assert (tmp_path / "spool" / "schedules" / f"{sid}.json").exists()


def test_cancelling_a_schedule_journals_the_status(client, tmp_path):
    sid = client.post("/api/schedule", json={
        "project_path": DEMO, "prompt": "p", "mode": "background",
        "scheduled_for": 2_000_000_000,
    }).json()["id"]

    client.delete(f"/api/schedule/{sid}")

    records = list((tmp_path / "spool" / "schedules").glob(f"{sid}.*.status.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["status"] == "cancelled"


def test_editing_a_schedule_rejournals_the_new_prompt(client, tmp_path):
    sid = client.post("/api/schedule", json={
        "project_path": DEMO, "prompt": "original", "mode": "background",
        "scheduled_for": 2_000_000_000,
    }).json()["id"]

    client.patch(f"/api/schedule/{sid}", json={"prompt": "edited"})

    record = tmp_path / "spool" / "schedules" / f"{sid}.json"
    assert json.loads(record.read_text())["prompt"] == "edited"


def test_a_journal_failure_does_not_cost_the_user_the_schedule(
    client, tmp_path, monkeypatch
):
    """Reported, not raised -- matching `POST /api/handoff`."""
    from bridge import schedspool

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(schedspool, "journal", boom)

    r = client.post("/api/schedule", json={
        "project_path": DEMO, "prompt": "p", "mode": "background",
        "scheduled_for": 2_000_000_000,
    })

    assert r.status_code == 201
    assert r.json()["journaled"] is False


def test_running_a_schedule_now_journals_the_claim_before_firing(launch_app, tmp_path):
    """Use the `launch_app` fixture, not `client` -- this one actually fires."""
    app, recorder = launch_app
    c = TestClient(app)
    sid = c.post("/api/schedule", json={
        "project_path": DEMO, "prompt": "p", "mode": "background",
        "scheduled_for": 2_000_000_000,
    }).json()["id"]

    c.post(f"/api/schedule/{sid}/run-now")

    statuses = [
        json.loads(p.read_text())["status"]
        for p in (tmp_path / "spool" / "schedules").glob(f"{sid}.*.status.json")
    ]
    assert "launching" in statuses
    assert "fired" in statuses


def test_a_claim_that_cannot_be_journalled_does_not_fire(launch_app, tmp_path, monkeypatch):
    """The one journal failure that must abort: firing without the claim record
    is the duplicate-launch scenario this whole change exists to close."""
    from bridge import schedspool

    app, recorder = launch_app
    c = TestClient(app)
    sid = c.post("/api/schedule", json={
        "project_path": DEMO, "prompt": "p", "mode": "background",
        "scheduled_for": 2_000_000_000,
    }).json()["id"]

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(schedspool, "journal_status", boom)

    r = c.post(f"/api/schedule/{sid}/run-now")

    assert r.json()["status"] == "failed"
    assert recorder.calls == []
```

Read `tests/test_api.py:571-589` for `launch_app`'s real return shape and the
recorder's actual attribute name before writing these two — `recorder.calls` is
a placeholder for whatever that double exposes.

The `client` fixture's app must be built with the same `tmp_path` its config
uses; confirm by reading `tests/test_api.py:18-33` before writing these.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_api.py -q -k "journal"`
Expected: FAIL — no `journaled` key, no files written.

- [ ] **Step 3: Wire the creation sites**

Add `schedspool` to the `bridge` imports at the top of `src/bridge/api.py`.

In `post_schedule`, replace `store.create_scheduled_run(job)` and the return
with:

```python
        # Journal before the insert, matching `POST /api/handoff`: a failure is
        # reported rather than raised, because a filesystem problem must never
        # cost the user a schedule they just authored.
        journaled = True
        try:
            schedspool.journal(job, cfg.spool_dir)
        except OSError:
            log.exception("failed to journal scheduled run %r", job.id)
            journaled = False
        store.create_scheduled_run(job)
        return {**dict(store.get_scheduled_run(job.id)), "journaled": journaled}
```

In `retry_schedule`, immediately after the `if row is None:` block and before
`_fire_claimed_job`:

```python
        # The retry is a new row, so it needs its own creation record, and a
        # failure ABORTS rather than warns. Replay restores creation records and
        # ignores orphan status records, so a retry that fires without one
        # vanishes on database loss -- and the user, seeing the original still
        # failed, retries it again and launches the work twice.
        try:
            schedspool.journal(_row_to_scheduled_run(row), cfg.spool_dir)
        except OSError as exc:
            log.exception("failed to journal retry of scheduled run %r", id)
            store.finish_scheduled_run(
                row["id"], status="failed",
                error=f"could not journal the retry: {exc}",
            )
            return dict(store.get_scheduled_run(row["id"]))
```

Add this helper next to `_fire_claimed_job` in `src/bridge/api.py`:

```python
def _row_to_scheduled_run(row) -> ScheduledRun:
    """Rebuild the dataclass from a `sqlite3.Row` so it can be journalled.

    `retry_terminal` and the fire path both hand back rows rather than models;
    the journal stores `dataclasses.asdict`, so it needs the dataclass.
    `retry_of` must survive. Dropping it would let `retry_terminal`'s
    `NOT EXISTS (... retry_of = orig.id)` guard stop seeing the retry that
    already exists, so after a database loss the user could retry the same
    original a second time and launch the work twice.
    """
    fields = {f.name for f in dataclasses.fields(ScheduledRun)}
    return ScheduledRun(**{k: row[k] for k in row.keys() if k in fields})
```

Add `import dataclasses` to `src/bridge/api.py` if it is not already imported.

- [ ] **Step 4: Wire the status sites**

Both of these journal **before** the database changes. Journaling afterwards
leaves a window where the row is already cancelled or edited but the journal
still describes the old state, and a database loss in that window replays a
cancelled job as `pending` — fireable.

Replace the body of `patch_schedule` with:

```python
    @app.patch("/api/schedule/{id}")
    def patch_schedule(id: str, body: SchedulePatch):
        patch_fields = body.model_dump(exclude_unset=True)
        current = store.get_scheduled_run(id)
        if current is None or current["status"] != "pending":
            raise _unknown_or_conflict(id)
        # Journal the intended post-edit state first. The guard above means
        # `edit_pending` will almost certainly succeed; if it loses a race and
        # does not, the journal describes an edit that never landed, which only
        # matters after a database loss and only costs the prompt text.
        # A journal failure propagates, matching `PATCH /api/handoff/{id}`:
        # the journal must never lag the database it rebuilds.
        merged = _row_to_scheduled_run(current)
        for key, value in patch_fields.items():
            setattr(merged, key, value)
        schedspool.journal(merged, cfg.spool_dir)
        if not store.edit_pending(id, **patch_fields):
            raise _unknown_or_conflict(id)
        return dict(store.get_scheduled_run(id))
```

Replace the body of `delete_schedule` with:

```python
    @app.delete("/api/schedule/{id}")
    def delete_schedule(id: str):
        current = store.get_scheduled_run(id)
        if current is None or current["status"] != "pending":
            raise _unknown_or_conflict(id)
        # Journal the cancellation before it happens, and let a failure
        # propagate. Cancelling without the record is the dangerous ordering:
        # after a database loss the creation record alone replays the job as
        # `pending`, and a job the user cancelled would fire.
        schedspool.journal_status(id, "cancelled", now_epoch(), cfg.spool_dir)
        if not store.cancel_pending(id):
            raise _unknown_or_conflict(id)
        return dict(store.get_scheduled_run(id))
```

In `_fire_claimed_job`, journal the **claim** first. Insert immediately after
`id = row["id"]` at line 343, before the `effective_path` lookup:

```python
    # The claim record is what stops a duplicate launch. `claim_specific` has no
    # `scheduled_for` guard, so run-now can claim a job scheduled for tomorrow;
    # without this record a database lost mid-launch replays that job as
    # `pending` and the scheduler fires it again. Unlike every other journal
    # call here, a failure ABORTS -- firing without the record is exactly the
    # scenario this exists to prevent.
    try:
        schedspool.journal_status(id, "launching", now_epoch(), cfg.spool_dir)
    except OSError as exc:
        log.exception("failed to journal claim of scheduled run %r", id)
        store.finish_scheduled_run(
            id, status="failed", error=f"could not journal the claim: {exc}"
        )
        return store.get_scheduled_run(id)
```

Then, for the outcome, replace the `return store.get_scheduled_run(id)` at
line 384 with:

```python
    final = store.get_scheduled_run(id)
    if final is not None:
        # One call for all three outcomes rather than three at each
        # `finish_scheduled_run`: the row's own status is the authority, and a
        # journal failure here is demoted because a launched session is not
        # undone by a filesystem error.
        try:
            schedspool.journal_status(
                id, final["status"], now_epoch(), cfg.spool_dir
            )
        except OSError:
            log.exception("failed to journal terminal status of %r", id)
    return final
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_api.py -q`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q`

Two failure classes are expected and must be fixed, not suppressed:
1. Existing `POST /api/schedule` assertions comparing the whole response body now see the extra `journaled` key.
2. `tests/test_mutation_specs.py` anchors on `api.py` text this task moved.

Expected after repair: PASS, 708 tests

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add src/bridge/api.py tests/test_api.py tools/mutations/
/usr/bin/git commit -m "Journal scheduled-run creations and terminal statuses from the routes"
```

---

### Task 5: Journal at boot, and replay from `bridge index`

**Files:**
- Modify: `src/bridge/__main__.py:61-68` (index rebuild), `:97-113` (boot reconcile and prune)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `schedspool.journal_status`, `schedspool.rebuild_if_empty(store, spool_dir, now)`, `store.launching_scheduled_run_ids() -> list[str]`, `store.reconcile_launching(now, ids) -> int`, `store.prunable_scheduled_run_ids(before_epoch) -> list[str]`, `store.prune_scheduled_runs(ids) -> int`
- Produces: `bridge index` JSON output gains a `schedules_rebuilt` key.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py`, following `test_serve_prunes_finished_scheduled_runs_past_the_retention_window` at lines 87-119 for the stubbed-uvicorn pattern:

```python
def test_serve_journals_the_runs_it_prunes(serve_cfg, monkeypatch):
    """Without this record, replay resurrects every run retention ever reaped."""
    cfg, tmp_path = serve_cfg
    store = Store(cfg.db_path)
    store.create_scheduled_run(ScheduledRun(
        id="old", project_path=DEMO, prompt="p", mode="background",
        scheduled_for=100, created_at=100, status="fired",
    ))
    store.conn.execute(
        "UPDATE scheduled_runs SET completed_at=? WHERE id=?", (100, "old")
    )
    store.close()

    _run_serve(monkeypatch, cfg)

    records = list((cfg.spool_dir / "schedules").glob("old.*.status.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["status"] == "pruned"


def test_index_replays_the_schedule_journal(serve_cfg, monkeypatch):
    cfg, tmp_path = serve_cfg
    schedspool.journal(
        ScheduledRun(
            id="j1", project_path=DEMO, prompt="p", mode="background",
            scheduled_for=2_000_000_000, created_at=100,
        ),
        cfg.spool_dir,
    )

    out = _run_index(monkeypatch, cfg)

    assert out["schedules_rebuilt"] == 1


def test_a_run_whose_prune_cannot_be_journalled_is_not_deleted(
    serve_cfg, monkeypatch
):
    """Deleting without the marker is what lets replay resurrect history."""
    cfg, tmp_path = serve_cfg
    store = Store(cfg.db_path)
    store.create_scheduled_run(ScheduledRun(
        id="old", project_path=DEMO, prompt="p", mode="background",
        scheduled_for=100, created_at=100, status="fired",
    ))
    store.conn.execute(
        "UPDATE scheduled_runs SET completed_at=? WHERE id=?", (100, "old")
    )
    store.close()

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(schedspool, "journal_status", boom)
    _run_serve(monkeypatch, cfg)

    survivor = Store(cfg.db_path)
    try:
        assert survivor.get_scheduled_run("old") is not None
    finally:
        survivor.close()
```

**`serve_cfg`'s real shape — verified.** It returns `(launches, served)`, **not**
`(cfg, tmp_path)`, and it does not return `cfg` at all (`tests/test_main.py:23-43`).
The config it installs is built from `tmp_path`, so derive paths directly:

- database → `tmp_path / "s.db"`
- spool → `tmp_path / "spool"`, so schedule records are at `tmp_path/"spool"/"schedules"`

It monkeypatches `entry.load` and `uvicorn.run`, so driving a boot is just
`entry.main(["serve"])`. Rewrite the three snippets above against that shape,
take `tmp_path` as a fixture argument, and add the imports they need — `json`,
`Store`, `ScheduledRun`, `schedspool`, and a `DEMO` path constant are all
referenced above and none are guaranteed to be in that module already. Check the
file's existing imports first.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_main.py -q -k "journals_the_runs or replays_the_schedule"`
Expected: FAIL — no `schedules` directory written, no `schedules_rebuilt` key.

- [ ] **Step 3: Journal the boot operations**

In `src/bridge/__main__.py`, add `schedspool` to the `from bridge import ...`
line that already imports `spool`. Then extend the two boot blocks:

Both blocks read ids, journal, then mutate — in that order. A row whose journal
write fails is **left alone** rather than mutated without a record.

```python
    # Journal before flipping, and flip only what was journalled. An unjournalled
    # row must be LEFT `launching`: a future job claimed by run-now, flipped
    # without its record and then lost to a database failure, replays as
    # `pending` and fires a second time -- the exact hole this change closes.
    # Left alone, the next boot reconciles it again.
    reconcilable = []
    for run_id in store.launching_scheduled_run_ids():
        try:
            schedspool.journal_status(
                run_id, "indeterminate", now_epoch(), cfg.spool_dir
            )
        except OSError:
            log.exception("failed to journal reconcile of %r; leaving it", run_id)
            continue
        reconcilable.append(run_id)
    stray = store.reconcile_launching(now_epoch(), reconcilable)
    if stray:
        log.info("reconciled %d stray 'launching' scheduled run(s)", stray)

    # Journal before deleting, and skip any row we could not journal. Without
    # the `pruned` record the row is gone from the database but still a creation
    # record on disk, and the next rebuild brings it back. Retention can wait a
    # boot; journal integrity cannot.
    reapable = []
    for run_id in store.prunable_scheduled_run_ids(
        now_epoch() - SCHEDULED_RUN_RETENTION_DAYS * 86400
    ):
        try:
            schedspool.journal_status(run_id, "pruned", now_epoch(), cfg.spool_dir)
        except OSError:
            log.exception("failed to journal prune of %r; leaving it", run_id)
            continue
        reapable.append(run_id)
    reaped = store.prune_scheduled_runs(reapable)
    if reaped:
        log.info("pruned %d finished scheduled run(s)", reaped)
```

This ordering is safe at boot because it runs single-threaded before the
scheduler thread starts (`__main__.py:121-124`), so nothing can mutate a row
between the id read and the mutation.

- [ ] **Step 4: Replay from `bridge index`**

In the `index` branch, after the existing `handoffs_rebuilt` line:

```python
        stats["schedules_rebuilt"] = schedspool.rebuild_if_empty(
            store, cfg.spool_dir, now_epoch()
        ).restored
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_main.py -q`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q`
Expected: PASS, 710 tests

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add src/bridge/__main__.py tests/test_main.py
/usr/bin/git commit -m "Journal boot reconcile and prune, and replay schedules from bridge index"
```

---

### Task 6: Mutation spec and end-to-end verification

**Files:**
- Create: `tools/mutations/schedule-spool.json`
- Test: the whole suite

**Interfaces:**
- Consumes: everything above.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Read an existing spec to copy its exact shape**

Run: `/bin/cat tools/mutations/scheduled-retry.json`

The `old` strings must match the source byte for byte, including indentation.

- [ ] **Step 2: Write the mutation spec**

Create `tools/mutations/schedule-spool.json` with four mutations. Each `old`
must be copied verbatim from the current source — retype nothing from memory:

1. **Empty-table guard removed.** In `src/bridge/schedspool.py`, change
   `if store.count_scheduled_runs() > 0:` to `if False:`.
   Caught by `test_replay_is_skipped_when_the_table_is_not_empty`.
2. **Pruned skip removed.** Change `if job.id in pruned:` to `if False:`.
   Caught by `test_a_pruned_job_does_not_come_back_and_a_past_due_one_is_missed`.
3. **Missed boundary removed.** Change `elif job.scheduled_for > when:` to
   `elif True:`, so every past-due job replays as `pending` and would fire.
   Caught by the same test's `stale` assertion. Do not mutate `>` to `>=`
   instead — the test data sits an hour either side of `now`, so an off-by-one
   on the boundary is genuinely equivalent there and would survive.
4. **Creation journal dropped.** In `src/bridge/api.py`, change
   `schedspool.journal(job, cfg.spool_dir)` to `pass`.
   Caught by `test_creating_a_schedule_journals_it`.
5. **Claim replays as its raw status.** In `src/bridge/schedspool.py`, change
   `"indeterminate" if ended.status == "launching" else ended.status` to
   `ended.status`, so a claim record restores the job as `launching` — which
   `reconcile_launching` would then flip, but only on a `serve` that may not
   come before the next tick.
   Caught by `test_a_future_job_claimed_by_run_now_replays_as_indeterminate`.
6. **Prune journals after deleting.** In `src/bridge/__main__.py`, move the
   `reapable.append(run_id)` above the `try:` so a failed journal no longer
   skips the row.
   Caught by `test_a_run_whose_prune_cannot_be_journalled_is_not_deleted`.

- [ ] **Step 3: Commit before running the harness**

The harness requires a committed clean tree.

```bash
/usr/bin/git add tools/mutations/schedule-spool.json
/usr/bin/git commit -m "Pin the scheduled-run journal with a mutation spec"
```

- [ ] **Step 4: Run the mutation harness**

Run: `/Users/mitsheth/.local/bin/uv run python tools/falsify.py --spec tools/mutations/schedule-spool.json`
Expected: 6/6 caught.

A survivor means the test asserting that behavior is vacuous. Fix the **test**,
not the mutation — but check first whether the mutation is genuinely equivalent
to the original, which has happened twice in this repo. If it is, delete it and
say so.

- [ ] **Step 5: Run every mutation spec and the full suite**

```bash
/Users/mitsheth/.local/bin/uv run pytest -q
/Users/mitsheth/.local/bin/uv run python tools/falsify.py --spec tools/mutations/scheduled-runs.json
/Users/mitsheth/.local/bin/uv run python tools/falsify.py --spec tools/mutations/scheduled-retry.json
/Users/mitsheth/.local/bin/uv run python tools/falsify.py --spec tools/mutations/task1-store-and-spool.json
```

Expected: suite green; all three existing specs still fully caught. The last one
proves `spool.py` was left byte-identical.

- [ ] **Step 6: Verify the real recovery path by hand**

```bash
/usr/bin/git status --short          # must be clean
/bin/ls ~/.bridge/spool/schedules/   # records from real panel use, if any
```

Do **not** delete the real `~/.bridge/bridge.db`. The behavior is covered by
`test_the_journal_survives_a_real_database_deletion` against a tmp database.

- [ ] **Step 7: Commit any test repairs**

```bash
/usr/bin/git add -A
/usr/bin/git commit -m "Repair mutation anchors after the schedule journal"
```

Skip if the tree is already clean.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `schedspool.py` module, imports from `spool` without editing it | 1 |
| `schedules/` directory, separate from `drained/` | 1 |
| `journal`, `journal_status`, `STATUS_SUFFIX` | 1 |
| `RebuildStats` | 1 (declared), 2 (populated) |
| conftest guard tuple | 1, extended in 2 |
| Five replay rules, empty-table guard | 2 |
| `restore_scheduled_run` so terminal rows carry `completed_at` | 2 (Step 3b) |
| Status vocabulary validation | 1 (`_load_status`), 2 (test) |
| `missed` in the vocabulary | 2 |
| `pruned` journal-only | 2 (skip logic), 5 (written at boot) |
| Claim journaled; replay resolves it to `indeterminate` | 2 (rule 3), 4 (write site) |
| `retry_terminal` accepts `missed` | 3 |
| Read-only id companions; journal-before-mutate | 3 (store), 5 (boot ordering) |
| `store.py:396-397` comment | 3 |
| Nine journal call sites | 4 (six), 5 (two), plus `bridge index` replay |
| Per-site error policy | 4, 5 |
| `bridge index` replay | 5 |
| `tests/test_schedspool.py` | 1, 2 |
| Mutation spec | 6 |
| Existing anchors re-verified | 3, 4, 6 |

No gaps.

**Type consistency:** `journal(job, spool_dir)`, `journal_status(run_id, status, at, spool_dir)`,
`rebuild_if_empty(store, spool_dir, now)` — `now` required, no default —
`_dirs -> (schedules_dir, bad_dir)`,
`RebuildStats(restored, missed, skipped_pruned, bad, skipped)`, `_Status(run_id, status, at)` are
used identically in every task. The status record's key is `run_id` throughout — not `handoff_id`,
and not `schedule_id`. The store signatures after Task 3 are exactly:
`prunable_scheduled_run_ids(before_epoch) -> list[str]`,
`prune_scheduled_runs(ids) -> int`,
`launching_scheduled_run_ids() -> list[str]`,
`reconcile_launching(now, ids) -> int`,
`restore_scheduled_run(job) -> str`.
Only the two `*_ids` readers return lists; both mutators return counts.

**Second revision note.** A follow-up Codex review of the first revision found seven more issues,
three blocking, all now fixed above: `bridge index` called `rebuild_if_empty` without the required
`now`; boot reconcile journaled per row but then flipped *every* `launching` row, so an unjournaled
one was mutated anyway; `PATCH` and `DELETE` journaled after mutating, so a cancelled job could
replay as `pending` and fire. Also fixed: `retry_of` was dropped on replay, which would let
`retry_terminal` grant a second retry of an already-retried original; a retry could fire without a
durable creation record; `_load` did not type-check `scheduled_for`, so one bad record aborted the
whole replay instead of being quarantined; and the planned tests unpacked `client`, `launch_app` and
`serve_cfg` incorrectly — all three yield tuples, and `serve_cfg` does not return `cfg` at all.

**First revision note.** Tasks 2–6 were revised after a Codex review found that journaling only creations
and terminal statuses permitted a duplicate launch: `claim_specific` has no `scheduled_for` guard,
so a job run-now'd before its scheduled time and then lost to a database failure replayed as
`pending` and fired twice. The claim is journaled now. The same review found that bulk prune
journaled after mutating, that replayed terminal rows carried a NULL `completed_at` and so were
never reapable, and that three existing tests assert integer returns from the changed methods. All
four are addressed above.

**Known risk carried forward:** Task 3 Step 8 and Task 4 Step 6 both expect mutation-anchor drift
in `scheduled-runs.json` and `scheduled-retry.json`. That is anticipated, not a surprise — but an
implementer who weakens a mutation to make the anchor check pass has silently deleted coverage. The
instruction in both steps is to preserve the mutation's intent.
