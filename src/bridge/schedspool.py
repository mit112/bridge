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
import logging
from dataclasses import dataclass
from pathlib import Path

from bridge.models import ScheduledRun
from bridge.spool import _atomic_write, _quarantine

log = logging.getLogger(__name__)

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
                except Exception as exc:  # noqa: BLE001 - one bad record cannot stop replay
                    log.warning(
                        "quarantining unparsable schedule status record %s: %s",
                        path.name,
                        exc,
                    )
                    _quarantine(path, bad_dir)
                    stats.bad += 1
                continue
            try:
                records.append(_load(path))
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "quarantining unparsable schedule record %s: %s", path.name, exc
                )
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
