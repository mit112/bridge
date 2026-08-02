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
