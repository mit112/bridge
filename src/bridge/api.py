"""FastAPI application. Read-only in Phase 1; Phase 2 adds handoff capture.

This process stays the sole writer. The `bridge` CLI speaks HTTP to it and never
opens the database. Phase 3 makes it the sole *spawner* too: the card and the CLI
are both thin clients of `POST /api/launch`, and neither imports `launcher`.
"""

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator, model_validator

from bridge import launcher, spool
from bridge.cards import build_cards
from bridge.config import Config
from bridge.indexer import reindex
from bridge.models import Handoff
from bridge.registry import display_name, resolve_project
from bridge.store import Store, now_epoch

HERE = Path(__file__).parent

HandoffStatus = Literal["queued", "consumed", "dismissed", "superseded"]

# The spawner, injected into `create_app` with a default. Not testability polish:
# `launcher.launch` shells out to `/usr/bin/osascript`, which opens a real
# Terminal window running a real, token-burning session whose transcript the
# indexer then ingests. Injection is what makes it impossible for a route test to
# spawn one by accident, and it is why no test monkeypatches a module global.
LaunchFn = Callable[..., launcher.LaunchResult]


class HandoffIn(BaseModel):
    """The CLI mints `id`, which is what makes a re-drained spool file collide
    on the primary key instead of inserting a duplicate."""

    id: str
    project_path: str
    next_prompt: str
    session_id: str | None = None
    summary: str | None = None
    suggested_model: str | None = None
    suggested_effort: str | None = None
    created_at: int | None = None


class HandoffPatch(BaseModel):
    """`status`, `next_prompt`, or both — but never neither.

    Both fields are optional because the panel edits a prompt without touching
    the status and dismisses a handoff without touching its text. Optional fields
    alone, though, make `PATCH {}` a 200 that changes nothing, so the validator
    below rejects the empty body outright: a silent no-op is indistinguishable
    from a saved edit at the far end of a `fetch()`.
    """

    status: HandoffStatus | None = None
    next_prompt: str | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if self.status is None and self.next_prompt is None:
            raise ValueError("supply status, next_prompt, or both")
        return self


class LaunchIn(BaseModel):
    """`prompt` is optional, and that is the load-bearing part.

    `bridge launch` deliberately sends no prompt: the server already holds the
    queued handoff, so round-tripping it out to the client and back would be
    bytes over the wire — and a second copy to keep in sync — for nothing. When
    it is omitted the server uses that project's queued handoff, and having
    neither is an error rather than an empty session.
    """

    project_path: str
    prompt: str | None = None
    mode: str = "terminal"
    model: str | None = None
    effort: str | None = None
    handoff_id: str | None = None
    title: str | None = None

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        # Validated here rather than left to `launch()` so the check does not
        # depend on which launcher is injected, and consumed from `launcher.MODES`
        # rather than restated, so the vocabulary has one source.
        if value not in launcher.MODES:
            raise ValueError(f"mode must be one of {launcher.MODES}")
        return value


def create_app(
    store: Store, cfg: Config, launch_fn: LaunchFn = launcher.launch
) -> FastAPI:
    app = FastAPI(title="Bridge")

    # Drain before serving. Under the manual-`bridge serve` uptime model this is
    # the main way handoffs arrive, so it runs on every boot.
    #
    # `OSError` and not `Exception`: an unreadable or missing spool must not stop
    # the panel, but a *programming* error in the drain must not be swallowed
    # either. A catch-all here silently absorbed a test guard, and would just as
    # silently leave handoffs accumulating in the spool while cards showed none.
    try:
        app.state.boot_drain = asdict(spool.drain(store, cfg.spool_dir))
    except OSError as exc:
        app.state.boot_drain = {"error": repr(exc)}
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["ago"] = _ago
    templates.env.filters["ago_epoch"] = _ago_epoch
    templates.env.filters["kilo"] = _kilo
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        cards = build_cards(store, cfg)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "cards": cards,
                "totals": {
                    "today": sum(c.tokens_today for c in cards),
                    "last_5h": sum(c.tokens_5h for c in cards),
                    "projects": len(cards),
                },
            },
        )

    @app.get("/project/{project_id}", response_class=HTMLResponse)
    def detail(request: Request, project_id: int):
        row = store.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown project")
        return templates.TemplateResponse(
            request,
            "project.html",
            {
                "project": row,
                "sessions": store.sessions(project_id),
                "handoffs": store.handoffs(project_id),
            },
        )

    @app.get("/api/projects")
    def projects():
        return [dict(r) for r in store.projects()]

    @app.post("/api/refresh")
    def refresh():
        return asdict(reindex(store, cfg))

    @app.post("/api/handoff", status_code=201)
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
            journaled = False
        # Resolve through the alias table, then upsert: a handoff may arrive from
        # a project that was never indexed, or from an old ~/Documents path.
        project_id = resolve_project(store, h.project_path)
        store.create_handoff(h, project_id)
        return {"id": h.id, "project_id": project_id, "journaled": journaled}

    @app.get("/api/handoff")
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

    @app.get("/api/handoff/{project_id}")
    def get_handoff(project_id: int):
        row = store.queued_handoff(project_id)
        if row is None:
            return Response(status_code=204)
        return dict(row)

    @app.patch("/api/handoff/{handoff_id}")
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

        return dict(store.get_handoff(handoff_id))

    @app.post("/api/launch")
    def post_launch(body: LaunchIn):
        """Spawn one session. A failed *launch* is a 200, not an HTTP error.

        The boundary is whether a `launches` row exists yet:

        * Nothing recorded — an unknown mode, a NUL or oversize prompt, no
          `claude` on `PATH`, no prompt and nothing queued — is a `422`. Nothing
          happened, there is no launch id or outcome to report, and a
          `200 {outcome: 'failed'}` would describe a launch the database does not
          have.
        * A row exists and the *spawn* failed: `200`, `outcome='failed'`, and a
          non-empty `error`. The panel needs the error text and the prompt in the
          same response to show one and copy the other, and a 500 gives it
          neither. The handoff stays queued, so nothing is lost.
        """
        # Read-only resolution, matching `GET /api/handoff`: looking for a queued
        # prompt must not bring a project row into existence, or a launch that is
        # about to be refused would leave one behind. Canonicalising the project
        # for the launch itself is `launch()`'s job, through the same alias table.
        canonical = store.alias_map().get(body.project_path, body.project_path)
        project = store.project_by_path(canonical)
        queued = store.queued_handoff(project["id"]) if project else None

        prompt, handoff_id = body.prompt, body.handoff_id
        if prompt is None:
            if queued is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"no prompt supplied and nothing queued for {canonical}",
                )
            prompt, handoff_id = queued["next_prompt"], queued["id"]

        # Passing the handoff id is what gets it consumed and journalled, and only
        # on success — `launch()` leaves it queued when the spawn fails.
        handoff = store.get_handoff(handoff_id) if handoff_id else None
        if handoff_id and handoff is None:
            # `launches.handoff_id` has a foreign key, so an id the client made up
            # would otherwise surface as an IntegrityError traceback from `launch()`.
            raise HTTPException(status_code=404, detail="unknown handoff")
        spec = launcher.LaunchSpec(
            project_path=body.project_path,
            prompt=prompt,
            model=body.model,
            effort=body.effort,
            title=body.title or launcher.default_title(
                handoff["summary"] if handoff else None, display_name(canonical)
            ),
            mode=body.mode,
        )
        try:
            result = launch_fn(store, cfg, spec, handoff_id)
        except launcher.LaunchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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

    return app


def _ago(iso: str | None) -> str:
    """Compact relative time: 4m, 3h, 2d. Empty when unknown."""
    from bridge.store import now_epoch, to_epoch

    epoch = to_epoch(iso)
    if epoch is None:
        return ""
    secs = max(0, now_epoch() - epoch)
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _ago_epoch(epoch: int | None) -> str:
    """Same shape as `ago`, for the epoch ints GitState carries."""
    from bridge.store import now_epoch

    if not epoch:
        return ""
    secs = max(0, now_epoch() - int(epoch))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _kilo(n: int | None) -> str:
    """Token counts as absolute magnitudes; never a percentage of a limit."""
    n = n or 0
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1_000_000:.1f}M"
