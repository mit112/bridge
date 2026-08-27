"""The handoff surface, and the launch route that consumes one.

Kept in one module because they are two halves of a single contract: a handoff
is captured here, and `POST /api/launch` is the only thing that consumes it.
Splitting them would put the id that ties them together across a module
boundary for no gain.

An `APIRouter` factory rather than a module-level router, for the same reason
as `routes_schedule`: every handler closes over the store, config, and injected
launcher that `create_app` owns, and `notify` stays a callable so the
`app.state.notifier` lookup is as lazy as it was inline.
"""

import logging
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Query, Response

from bridge import launcher, spool
from bridge.config import Config
from bridge.firing import LaunchFn, fire
from bridge.models import Handoff
from bridge.registry import display_name, resolve_project
from bridge.schemas import HandoffIn, HandoffPatch, LaunchIn
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

    @router.post("/api/handoff", status_code=201)
    def post_handoff(body: HandoffIn):
        h = Handoff(
            id=body.id,
            project_path=body.project_path,
            next_prompt=body.next_prompt,
            source_session_id=body.session_id,
            summary=body.summary,
            suggested_model=body.suggested_model,
            suggested_effort=body.suggested_effort,
            created_at=body.created_at or now_epoch(),
        )
        # Journal before inserting, so a handoff is recoverable from the moment
        # it is acknowledged. A journal failure must not cost the user the
        # prompt, so it is reported rather than raised.
        journaled = True
        try:
            spool.journal(h, cfg.spool_dir)
        except Exception:  # noqa: BLE001
            log.exception("failed to journal handoff %r", h.id)
            journaled = False
        # Resolve through the alias table, then upsert: a handoff may arrive from
        # a project that was never indexed, or from an old ~/Documents path.
        project_id = resolve_project(store, h.project_path)
        store.create_handoff(h, project_id)
        notify()
        return {"id": h.id, "project_id": project_id, "journaled": journaled}

    @router.get("/api/handoff")
    def get_handoff_by_path(project_path: str):
        """Lookup by path, for `bridge next`, which knows a cwd and not an id.

        Declared as a separate collection route rather than a `/{project_id}`
        variant, because a non-numeric segment there would 422 instead of
        resolving. Unlike POST this never upserts: a read must not bring a
        project row into existence.
        """
        canonical = store.alias_map().get(project_path, project_path)
        project = store.project_by_path(canonical)
        if project is None:
            return Response(status_code=204)
        row = store.queued_handoff(project["id"])
        if row is None:
            return Response(status_code=204)
        return dict(row)

    @router.get("/api/handoff/{project_id}")
    def get_handoff(project_id: int):
        row = store.queued_handoff(project_id)
        if row is None:
            return Response(status_code=204)
        return dict(row)

    @router.get("/api/handoffs")
    def list_handoffs_by_path(project_path: str):
        """Every queued handoff for a project, for the panel's stacked view.
        Returns [] (not 204) for an unknown or empty project, so the client
        renders 'nothing queued' without special-casing a no-content status."""
        canonical = store.alias_map().get(project_path, project_path)
        project = store.project_by_path(canonical)
        if project is None:
            return []
        return [dict(r) for r in store.queued_handoffs(project["id"])]

    @router.get("/api/handoffs/{project_id}")
    def list_handoffs(project_id: int):
        return [dict(r) for r in store.queued_handoffs(project_id)]

    @router.patch("/api/handoff/{handoff_id}")
    def patch_handoff(handoff_id: str, body: HandoffPatch):
        """Edit the queued prompt, change the status, or both.

        Each change is journalled *before* the row is written, exactly as the
        launcher journals consumption: the journal is what survives
        `rm ~/.bridge/bridge.db`, so it must never lag the database it rebuilds.
        A journal failure therefore surfaces with the row untouched, rather than
        leaving a saved edit the journal has never heard of.
        """
        row = store.get_handoff(handoff_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown handoff")

        if body.next_prompt is not None:
            project = store.get_project(row["project_id"])
            spool.journal(
                Handoff(
                    id=handoff_id,
                    project_path=project["path"],
                    next_prompt=body.next_prompt,
                    source_session_id=row["source_session_id"],
                    summary=row["summary"],
                    suggested_model=row["suggested_model"],
                    suggested_effort=row["suggested_effort"],
                    created_at=row["created_at"],
                    status=row["status"],
                ),
                cfg.spool_dir,
            )
            # Status untouched: editing a queued prompt leaves it queued. The
            # exact bytes each launch ran are kept separately in
            # `launches.prompt`, so an edit cannot erase what actually ran.
            store.update_handoff_prompt(handoff_id, body.next_prompt)

        if body.status is not None:
            spool.journal_status(handoff_id, body.status, now_epoch(), cfg.spool_dir)
            store.set_handoff_status(handoff_id, body.status)

        notify()
        return dict(store.get_handoff(handoff_id))

    @router.post("/api/launch")
    def post_launch(body: LaunchIn):
        """Spawn one session. A failed *launch* is a 200, not an HTTP error.

        The boundary is whether a `launches` row exists yet:

        * Nothing recorded — an unknown mode, a NUL or oversize prompt, neither
          a prompt nor a handoff_id — is a `422`. Nothing happened, there is no
          launch id or outcome to report, and a `200 {outcome: 'failed'}` would
          describe a launch the database does not have.
        * A row exists and the *spawn* failed: `200`, `outcome='failed'`, and a
          non-empty `error`. The panel needs the error text and the prompt in the
          same response to show one and copy the other, and a 500 gives it
          neither. The handoff stays queued, so nothing is lost.
        """
        # Canonicalising the project path is needed for the title default
        # below, matching `GET /api/handoff`'s alias resolution; the launch
        # itself canonicalises again through the same alias table, inside
        # `launch()`.
        canonical = store.alias_map().get(body.project_path, body.project_path)

        prompt, handoff_id = body.prompt, body.handoff_id
        if prompt is None and handoff_id is None:
            # A project may have several queued handoffs; grabbing one at
            # random would fire whichever happened to be newest instead of
            # the one the caller meant, so the target must be explicit.
            raise HTTPException(
                status_code=422,
                detail="supply a prompt or a handoff_id; a project may have "
                       "several queued handoffs and the target must be explicit",
            )

        # Fetched once and reused below for both the prompt (when the caller
        # sent a handoff_id but no prompt) and the launch title, so an explicit
        # handoff_id doesn't hit the store twice for the same row. Passing the
        # id on to `fire` is what gets it consumed and journalled, and only on
        # success — `launch()` leaves it queued when the spawn fails.
        handoff = store.get_handoff(handoff_id) if handoff_id else None
        if handoff_id and handoff is None:
            # `launches.handoff_id` has a foreign key, so an id the client made
            # up would otherwise surface as an IntegrityError traceback from
            # `launch()`.
            raise HTTPException(status_code=404, detail="unknown handoff")
        if prompt is None:
            prompt = handoff["next_prompt"]
        title = body.title or launcher.default_title(
            handoff["summary"] if handoff else None, display_name(canonical)
        )
        try:
            result = fire(
                store, cfg,
                project_path=body.project_path,
                prompt=prompt,
                mode=body.mode,
                model=body.model,
                effort=body.effort,
                permission_mode=body.permission_mode,
                title=title,
                handoff_id=handoff_id,
                launch_fn=launch_fn,
            )
        except launcher.LaunchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        notify()
        return {
            "launch_id": result.launch_id,
            "outcome": result.outcome,
            "session_id": result.session_id,
            "short_id": result.short_id,
            "error": result.error,
            "note": result.note,
            "handoff_id": handoff_id,
            # Echoed so a client that never sent the prompt — `bridge launch`, or
            # a card rendered before someone else edited it — can still put the
            # right bytes on the clipboard when the launch fails.
            "prompt": prompt,
        }

    return router
