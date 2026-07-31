"""FastAPI application. Read-only in Phase 1."""

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bridge.cards import build_cards
from bridge.config import Config
from bridge.indexer import reindex
from bridge.store import Store

HERE = Path(__file__).parent


def create_app(store: Store, cfg: Config) -> FastAPI:
    app = FastAPI(title="Bridge")
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
            {"project": row, "sessions": store.sessions(project_id)},
        )

    @app.get("/api/projects")
    def projects():
        return [dict(r) for r in store.projects()]

    @app.post("/api/refresh")
    def refresh():
        return asdict(reindex(store, cfg))

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
