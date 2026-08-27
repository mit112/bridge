"""The `/api/schedule` surface: create, list, edit, cancel, fire early, retry.

A schedule is only ever fired FOR REAL by `scheduler.tick`; `run-now` exists so
the panel can test one (or just stop waiting for it) without a second code path
to keep in sync with the scheduler's own. Both ends meet in
`firing._fire_claimed_job`.

Split out of `create_app` as an `APIRouter` factory rather than a module-level
router, because every handler closes over the store, config, and injected
launcher that `create_app` owns -- `notify` is passed as a callable so the
`app.state.notifier` lookup stays lazy, exactly as it was inline.
"""

import logging
from collections.abc import Callable
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Response

from bridge import schedspool
from bridge.config import Config
from bridge.firing import LaunchFn, _fire_claimed_job, _row_to_scheduled_run
from bridge.models import ScheduledRun
from bridge.registry import resolve_project
from bridge.schemas import ScheduleIn, SchedulePatch
from bridge.store import Store, now_epoch

log = logging.getLogger(__name__)


def build_router(
    *,
    store: Store,
    cfg: Config,
    launch_fn: LaunchFn,
    notify: Callable[[], None],
) -> APIRouter:
    router = APIRouter()


    # --- scheduled runs -------------------------------------------------------
    #
    # A schedule is created, listed, edited, cancelled, or fired early -- but
    # only ever fired FOR REAL by the background scheduler. `run-now`
    # exists so the panel can test a schedule (or just stop waiting for it)
    # without a second code path to keep in sync with the scheduler's own.

    @router.post("/api/schedule", status_code=201)
    def post_schedule(body: ScheduleIn):
        project_path = store.alias_map().get(body.project_path, body.project_path)
        # Mirrors `post_launch`'s check for `body.handoff_id`: without it a
        # made-up id only fails at fire time, deep inside `_fire_claimed_job`,
        # as a foreign-key error instead of a 404 at the edge. The project_id
        # match additionally catches a handoff id that exists but belongs to
        # a DIFFERENT project -- scheduling it under this one would otherwise
        # fire someone else's prompt under this project's path. Ownership is
        # all that is checked here; whether it is still queued is left to the
        # atomic claim inside `launcher.launch()` at actual fire time, so an
        # edit or a manual launch between now and then is still caught then.
        if body.source_handoff_id is not None:
            source = store.get_handoff(body.source_handoff_id)
            if source is None:
                raise HTTPException(status_code=404, detail="unknown handoff")
            if source["project_id"] != resolve_project(store, project_path):
                raise HTTPException(
                    status_code=404,
                    detail="handoff belongs to a different project",
                )
        permission_mode = body.permission_mode
        job = ScheduledRun(
            id=str(uuid4()),
            project_path=project_path,
            prompt=body.prompt,
            summary=body.summary,
            model=body.model,
            effort=body.effort,
            mode=body.mode,
            permission_mode=permission_mode,
            source_handoff_id=body.source_handoff_id,
            scheduled_for=body.scheduled_for,
            created_at=now_epoch(),
        )
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
        notify()
        return {**dict(store.get_scheduled_run(job.id)), "journaled": journaled}

    @router.get("/api/schedule")
    def get_schedule(
        response: Response,
        limit: int | None = Query(None, ge=1),
        offset: int = Query(0, ge=0),
    ):
        """Unpaged by default -- the panel's own fetch wants the whole list --
        with `limit`/`offset` for anything walking a long history. The total
        rides in a header so a page can tell "that is all of them" from "there
        is more" without a second request.
        """
        response.headers["X-Total-Count"] = str(store.count_scheduled_runs())
        return [dict(r) for r in store.scheduled_runs(limit=limit, offset=offset)]

    def _unknown_or_conflict(id: str) -> HTTPException:
        """404 for an id nothing ever created, 409 for one that exists but is
        no longer `pending` -- the same distinction `patch_project` draws for
        an unknown project, extended with the state a schedule alone has."""
        if store.get_scheduled_run(id) is None:
            return HTTPException(status_code=404, detail="unknown schedule")
        return HTTPException(status_code=409, detail="schedule is no longer pending")

    @router.patch("/api/schedule/{id}")
    def patch_schedule(id: str, body: SchedulePatch):
        # `exclude_unset` is what keeps an omitted field out of the SQL
        # entirely, rather than overwriting it with the model's own `None`
        # default -- the same reason `patch_project` and `patch_handoff` check
        # `is not None` per field instead of writing the whole body.
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
        notify()
        return dict(store.get_scheduled_run(id))

    @router.delete("/api/schedule/{id}")
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
        notify()
        return dict(store.get_scheduled_run(id))

    @router.post("/api/schedule/{id}/run-now")
    def run_now(id: str):
        row = store.claim_specific(id)
        if row is None:
            raise _unknown_or_conflict(id)
        result = dict(_fire_claimed_job(store, cfg, row, launch_fn))
        notify()
        return result

    @router.post("/api/schedule/{id}/retry")
    def retry_schedule(id: str):
        """Re-fire a failed or indeterminate run, keeping its provenance.

        The panel used to retry by POSTing `/api/launch` with the prompt copied
        out of the page and no `handoff_id` at all -- so retrying a schedule
        created from a handoff launched fine and left that handoff queued
        forever. Going through the store means the retry inherits
        `source_handoff_id` and gets consumed exactly like the original would
        have been.

        The new row is created already `launching`, so it arrives at
        `_fire_claimed_job` in the same state `claim_specific` and
        `claim_one_due` produce -- one firing path, not two.
        """
        row = store.retry_terminal(id, new_id=str(uuid4()))
        if row is None:
            if store.get_scheduled_run(id) is None:
                raise HTTPException(status_code=404, detail="unknown schedule")
            raise HTTPException(
                status_code=409,
                detail="only a failed or indeterminate run can be retried, once",
            )
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
            notify()
            return dict(store.get_scheduled_run(row["id"]))
        result = dict(_fire_claimed_job(store, cfg, row, launch_fn))
        notify()
        return result

    return router
