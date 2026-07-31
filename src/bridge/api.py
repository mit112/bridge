"""FastAPI application. Read-only in Phase 1; Phase 2 adds handoff capture.

This process stays the sole writer. The `bridge` CLI speaks HTTP to it and never
opens the database.
"""

from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from bridge import spool
from bridge.cards import build_cards
from bridge.config import Config
from bridge.indexer import reindex
from bridge.models import Handoff
from bridge.registry import resolve_project
from bridge.store import Store, now_epoch

HERE = Path(__file__).parent

HandoffStatus = Literal["queued", "consumed", "dismissed", "superseded"]


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


class StatusIn(BaseModel):
    status: HandoffStatus


def create_app(store: Store, cfg: Config) -> FastAPI:
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
    def patch_handoff(handoff_id: str, body: StatusIn):
        if store.get_handoff(handoff_id) is None:
            raise HTTPException(status_code=404, detail="unknown handoff")
        store.set_handoff_status(handoff_id, body.status)
        return dict(store.get_handoff(handoff_id))

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
