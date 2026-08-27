"""Turning an already-chosen prompt into a spawned session.

Split out of `api` because the scheduler needs this tail without needing a
FastAPI app: `POST /api/launch`, `POST /api/schedule/{id}/run-now`, and
`scheduler.tick` all end here, and `scheduler` previously had to import from
`api` -- pulling the whole route module in behind it -- to reach one function.
"""

import dataclasses
import logging
from collections.abc import Callable

from bridge import launcher, schedspool
from bridge.config import Config
from bridge.models import ScheduledRun
from bridge.registry import display_name
from bridge.store import Store, now_epoch

log = logging.getLogger(__name__)

# The spawner, injected into `create_app` with a default. Not testability polish:
# `launcher.launch` shells out to `/usr/bin/osascript`, which opens a real
# Terminal window running a real, token-burning session whose transcript the
# indexer then ingests. Injection is what makes it impossible for a route test to
# spawn one by accident, and it is why no test monkeypatches a module global.
LaunchFn = Callable[..., launcher.LaunchResult]


def fire(
    store: Store,
    cfg: Config,
    *,
    project_path: str,
    prompt: str,
    mode: str,
    model: str | None,
    effort: str | None,
    permission_mode: str | None,
    title: str | None,
    handoff_id: str | None,
    launch_fn: LaunchFn,
) -> launcher.LaunchResult:
    """Resolve the alias table, build a `LaunchSpec`, and spawn it.

    The one tail `POST /api/launch` and the scheduler both need: neither
    caller does prompt/handoff selection or journalling here -- those stay
    inside `launcher.launch` -- this is only the part that turns an already-
    chosen prompt into a spawn. A caller with no route in front of it (the
    scheduler) has nothing else that resolves an aliased path, so that
    resolution lives here rather than being duplicated at every call site.
    """
    effective_path = store.alias_map().get(project_path, project_path)
    spec = launcher.LaunchSpec(
        project_path=effective_path,
        prompt=prompt,
        model=model,
        effort=effort,
        title=title,
        mode=mode,
        permission_mode=permission_mode,
    )
    return launch_fn(store, cfg, spec, handoff_id)


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


def _fire_claimed_job(store: Store, cfg: Config, row, launch_fn: LaunchFn):
    """The shared tail after a scheduled run is claimed -- `POST
    /api/schedule/{id}/run-now` and the background scheduler both end here, with
    nothing else between "claimed" and "fired".

    `row` is the just-claimed snapshot (from `claim_specific` or
    `claim_one_due`): its own prompt/mode/handoff, not values re-read or
    reconstructed elsewhere, is what gets fired, so a concurrent edit to the
    still-pending original (impossible; claiming is what makes editing fail)
    can never race the run that already started.

    A schedule has no `title` column -- `summary` stands in for it, exactly as
    a handoff's summary stands in for `LaunchIn.title` in `post_launch`, and
    falls back to the same `launcher.default_title` call with the same
    arguments, so a scheduled and a manual launch title identically.
    """
    id = row["id"]
    # The claim record is what stops a duplicate launch. `claim_specific` has no
    # `scheduled_for` guard, so run-now can claim a job scheduled for tomorrow;
    # without this record a database lost mid-launch replays that job as
    # `pending` and the scheduler fires it again. Unlike every other journal
    # call here, a failure ABORTS -- firing without the record is exactly the
    # scenario this exists to prevent.
    try:
        schedspool.journal_status(id, "launching", now_epoch(), cfg.spool_dir)
    except OSError as exc:
        # Marking this `failed` would be a WORSE outcome than the filesystem
        # hiccup that caused it: `failed` is terminal, so a job still owed
        # tomorrow would never fire again over a transient write error today.
        # Handing the claim back to `pending` costs nothing the claim itself
        # hadn't already cost -- fire() was never called, so no launch was
        # skipped -- and leaves the job exactly where a retry (this run-now,
        # or the scheduler's own next tick past its time) can claim it again.
        log.exception("failed to journal claim of scheduled run %r", id)
        store.unclaim(id)
        return store.get_scheduled_run(id)
    effective_path = store.alias_map().get(row["project_path"], row["project_path"])
    title = row["summary"] or launcher.default_title(
        row["summary"], display_name(effective_path)
    )
    try:
        result = fire(
            store, cfg,
            project_path=row["project_path"],
            prompt=row["prompt"],
            mode=row["mode"],
            model=row["model"],
            effort=row["effort"],
            permission_mode=row["permission_mode"],
            title=title,
            handoff_id=row["source_handoff_id"],
            launch_fn=launch_fn,
        )
    except launcher.LaunchError as exc:
        store.finish_scheduled_run(id, status="failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - see docstring below
        # Anything that is not a `LaunchError` -- a bare `sqlite3.IntegrityError`
        # from `create_launch` if the handoff was deleted between schedule and
        # fire, or any other bug -- must not 500 `run-now` or leave the row
        # stuck `launching` until the next boot's reconcile. `indeterminate` is
        # the honest answer: the claim already happened, so we genuinely do not
        # know whether a session spawned, and unlike `failed` it is never
        # re-claimed, preserving the no-auto-retry guarantee.
        log.exception(
            "scheduled run %r raised an unexpected exception while firing", id
        )
        store.finish_scheduled_run(id, status="indeterminate", error=str(exc))
    else:
        if result.outcome == "started":
            store.finish_scheduled_run(
                id, status="fired", launch_id=result.launch_id, fired_at=now_epoch()
            )
        else:
            store.finish_scheduled_run(
                id, status="failed", launch_id=result.launch_id, error=result.error
            )
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
