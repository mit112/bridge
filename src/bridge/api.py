"""FastAPI application: the panel's routes, and the app factory that wires them.

This process is the sole writer and the sole spawner. The `bridge` CLI speaks
HTTP to it and never opens the database; the panel and the CLI are both thin
clients of `POST /api/launch`, and neither imports `launcher`.

The route groups themselves live in `routes_*` modules and are mounted here --
`create_app` owns the collaborators they close over (store, config, injected
launcher, notifier) and nothing else can hand those out.
"""

import json
import logging
import secrets
import threading
import time
from dataclasses import asdict
from dataclasses import replace as dataclasses_replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from bridge import __version__, agents, hooks, launcher, schedspool, setup, spool, update
from bridge.cards import (
    FIVE_HOURS,
    GitProbeCache,
    LivenessDebouncer,
    build_cards,
)
from bridge.config import Config
from bridge.dashboard import DashboardBuilder
from bridge import diagnostics
from bridge.filters import register_template_filters
from bridge.firing import LaunchFn, _fire_claimed_job, _row_to_scheduled_run, fire
from bridge.http_policy import (
    FRAGMENT_HEADER,
    LOOPBACK_HOSTNAMES,
    UNSAFE_METHODS,
    CachedStaticFiles,
    _hostname,
    _layout_for,
)
from bridge.models import AgentsState, Handoff, ScheduledRun
from bridge.notify import ChangeNotifier
from bridge.overview import build_overview
from bridge.projects_view import build_projects
from bridge.refresh import RefreshCoordinator
from bridge.registry import display_name, resolve_project
from bridge.routes_handoffs import build_router as build_handoffs_router
from bridge.routes_schedule import build_router as build_schedule_router
from bridge.schedule_view import build_schedule
from bridge.schemas import (
    HandoffIn,
    HandoffPatch,
    HandoffStatus,
    LaunchIn,
    ProjectPatch,
    ProjectStatus,
    UpdateIn,
)
from bridge.settings_view import build_settings
from bridge.store import Store, now_epoch
from bridge.workspace import build_workspace

HERE = Path(__file__).parent

log = logging.getLogger(__name__)

def create_app(
    store: Store, cfg: Config, launch_fn: LaunchFn = launcher.launch,
    refresh_coordinator: RefreshCoordinator | None = None,
    notifier: ChangeNotifier | None = None,
    update_checker: update.UpdateChecker | None = None,
) -> FastAPI:
    app = FastAPI(title="Bridge")

    @app.middleware("http")
    async def _fragment_never_cacheable(request: Request, call_next):
        # A fragment and its full document share a URL, differing only by the
        # request header `_layout_for` reads. Left cacheable, a browser can
        # store the headless fragment under the page URL and then hand it back
        # as the whole document on a back/forward navigation -- the page renders
        # unstyled (no <head>, no app.css) until a manual reload. Chrome's memory
        # cache keys on URL alone and ignores `Vary`, so `no-store` -- not `Vary`
        # -- is what actually keeps the fragment from ever being reused as a
        # document; `Vary` still marks the dependency for well-behaved caches.
        response = await call_next(request)
        if FRAGMENT_HEADER in request.headers:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "X-Bridge-Fragment"
        return response

    # Binding 127.0.0.1 does not decide which NAME the browser used to get here.
    # An attacker who points `evil.example` at 127.0.0.1 and gets the user to
    # open `http://evil.example:8787/` has a page the browser treats as
    # same-origin with the panel: `Origin` and `Host` both read
    # `evil.example:8787`, so the Origin check below sees them agree and allows
    # the write. That reaches `POST /api/launch` with
    # `permission_mode: bypassPermissions`, and every GET's body besides.
    #
    # Pinning `Host` to a loopback literal is what the Origin check cannot do
    # on its own: the attacker controls the name, but cannot make a browser
    # send `Host: 127.0.0.1` for a page served from their own domain. This runs
    # on reads too -- once the page is same-origin, the responses are readable.
    @app.middleware("http")
    async def _loopback_host_only(request: Request, call_next):
        host = request.headers.get("host")
        if host is not None and _hostname(host) not in LOOPBACK_HOSTNAMES:
            log.warning("refused non-loopback %s %s for host %r",
                        request.method, request.url.path, host)
            return JSONResponse({"detail": "non-loopback host refused"},
                                status_code=403)
        return await call_next(request)

    # Binding 127.0.0.1 keeps another machine out; it does NOT keep out a page
    # already in this machine's browser. A cross-origin `<form method=post>`
    # aimed at http://localhost:8787/api/refresh is a same-machine request, and
    # the body-less POSTs (`/api/refresh`, schedule `run-now`, schedule
    # `retry`) need no readable response to have already done their work --
    # they are exactly the shape a form post can reach.
    #
    # An Origin check is the entire fix. A browser sets the header on every
    # unsafe cross-origin request and a page cannot suppress it, while the CLI
    # and Claude Code's hook dispatcher are server-side HTTP clients that send
    # none at all -- so "absent" stays allowed and nothing off-browser changes.
    @app.middleware("http")
    async def _same_origin_writes_only(request: Request, call_next):
        origin = request.headers.get("origin")
        if (request.method in UNSAFE_METHODS and origin is not None
                and urlsplit(origin).netloc != request.headers.get("host")):
            log.warning("refused cross-origin %s %s from %r",
                        request.method, request.url.path, origin)
            return JSONResponse({"detail": "cross-origin write refused"},
                                status_code=403)
        response = await call_next(request)
        # Nothing here should ever be sniffed into another type. Bridge renders
        # user-controlled text (launch prompts, transcript excerpts, project
        # paths) and answers errors as JSON; a body reinterpreted as HTML is
        # the ordinary way either of those becomes script.
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    if refresh_coordinator is None:
        refresh_coordinator = RefreshCoordinator(store, cfg)
    app.state.refresh_coordinator = refresh_coordinator

    if notifier is None:
        notifier = ChangeNotifier()
    app.state.notifier = notifier

    if update_checker is None:
        # A disabled checker never touches the network, so route tests and a
        # panel built directly both get a valid `update` object for free.
        update_checker = update.UpdateChecker(enabled=cfg.update_check_enabled)
    app.state.update_checker = update_checker

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
        log.warning("boot drain failed: %s", exc)
        app.state.boot_drain = {"error": repr(exc)}
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    register_template_filters(templates.env)

    # The sidebar's connection/freshness readout is shell chrome -- every page
    # renders it, but only Overview builds the full dashboard model that carries
    # `freshness`. This computes the SAME `{server, index_at, index_age_seconds}`
    # projection the dashboard envelope does (bridge.dashboard._envelope), cheaply
    # and lazily at render time, so base.html's default `shell_status` block can
    # show it on Projects/Schedule/Settings/Diagnostics without each route
    # threading the value through its own context.
    def _shell_freshness() -> dict:
        now = int(time.time())
        status = refresh_coordinator.status_snapshot()
        index_at = status.index_at
        if index_at is None:
            latest = store.latest_index_run()
            if latest is not None:
                index_at = int(latest["ran_at"])
        age = max(0, now - index_at) if index_at is not None else None
        return {
            "server": "unavailable" if status.server == "unavailable" else "available",
            "index_at": index_at,
            "index_age_seconds": age,
        }

    templates.env.globals["shell_freshness"] = _shell_freshness
    templates.env.globals["update_token"] = update.read_or_create_token

    @app.exception_handler(StarletteHTTPException)
    async def _not_found(request: Request, exc: StarletteHTTPException):
        """A page URL gets a page; everything under `/api/` keeps its JSON.

        Split on the path, not on `Accept`: `/project/{id}` is an HTML route
        whichever client asked, and the JSON branch's `{"detail": ...}` shape
        is a contract the CLI and the panel's own fetches parse. Scoped to 404
        because that is the only status an HTML route raises -- anything else
        arriving here is unexpected and should keep the framework's own
        handling rather than be dressed up as a missing page.
        """
        if exc.status_code == 404 and not request.url.path.startswith("/api/"):
            return templates.TemplateResponse(
                request, "404.html",
                {"active": None, "path": request.url.path},
                status_code=404,
            )
        return await http_exception_handler(request, exc)

    app.mount("/static", CachedStaticFiles(directory=str(HERE / "static")), name="static")

    # One debouncer for the whole app, because the busy -> idle hold is state
    # ACROSS requests: a per-request instance would have nothing to remember
    # and the hysteresis would never fire.
    debouncer = LivenessDebouncer()

    # One git probe cache, for the same reason: a freshness window and a set of
    # in-flight refreshes are both cross-request state, and a per-request
    # instance would be cold on every page load -- which is the situation it
    # exists to end.
    git_cache = GitProbeCache(store)

    # --- hooks --------------------------------------------------------------
    #
    # The receiving end of `Notification` / `SessionStart` / `SessionEnd`,
    # posted by Claude Code as `type: "http"` hooks. This route is the ONLY
    # route to a `needs_input` state: no JSONL entry records a permission
    # prompt, so polling and transcript-tailing provably cannot see one.
    #
    # It must always answer, always fast, and never with an error. A hook that
    # fails is noise in somebody's unrelated session, and one that hangs stalls
    # a turn -- which is why the settings entries carry an explicit `timeout`
    # and why nothing below can raise.

    hook_state = app.state.hook_state = hooks.HookState()
    dashboard_builder = app.state.dashboard_builder = DashboardBuilder(
        store,
        cfg,
        refresh_coordinator,
        debouncer=debouncer,
        hook_state=hook_state,
        agents_fn=lambda: agents.probe(),
        git_cache=git_cache,
        now_fn=now_epoch,
    )

    @app.post("/api/hooks")
    async def post_hook(request: Request):
        try:
            event = await request.json()
        except Exception:  # noqa: BLE001 - a malformed body is still a 200
            return {"ok": True}
        try:
            hook_state.record(event)
        except Exception:  # noqa: BLE001
            pass
        app.state.notifier.bump()
        return {"ok": True}

    # --- SSE ----------------------------------------------------------------
    #
    # Snapshot on connect, then deltas WITH tombstones, a named `refresh` when
    # the server wants the client to resync over REST, and a capped stream that
    # lets `EventSource` reconnect rather than running an unbounded generator.
    #
    # There is deliberately no `Last-Event-ID` handling. Every reconnect opens
    # with a full snapshot, so there is nothing to replay -- which also deletes
    # the "requested event older than the retained window" branch entirely.
    #
    # Session status NEVER depends on client connectivity. Gating poll *cadence*
    # on connected clients would be fine; deriving *state* from it is the bug
    # class that produced three separate reported failures elsewhere.

    SSE_MAX_SECONDS = 300.0
    REBUILD_FLOOR_S = 0.2      # min seconds between builds; caps probe cost under storms
    MAX_SSE_CONNECTIONS = 32   # sync SSE connections each pin a threadpool worker

    _sse_connections = {"n": 0}
    _sse_lock = threading.Lock()

    def _frame(event: str, payload: dict) -> str:
        # The trailing BLANK line terminates the frame. With a single "\n" the
        # browser buffers forever and no event ever fires, with no error
        # anywhere -- which is why the tests assert on it explicitly.
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

    @app.get("/events")
    def events(max_ticks: int | None = None, interval: float = 3.0,
               floor: float = REBUILD_FLOOR_S, max_seconds: float = SSE_MAX_SECONDS):
        # `interval` keeps its name (many call sites already use it) but now
        # means the fallback wait timeout: the loop wakes on the notifier
        # instead of sleeping, and `interval` bounds how long it will wait
        # with no bump at all.
        with _sse_lock:
            if _sse_connections["n"] >= MAX_SSE_CONNECTIONS:
                return JSONResponse({"detail": "too many live connections"},
                                    status_code=503)
            _sse_connections["n"] += 1

        def live_signature(payload: dict) -> dict:
            # `queued`/`scheduled`/`dirty`/`attention` are all present in
            # EVERY payload shape this compares (both `full_update()` and the
            # faster `live_patch()` compute them the same way in
            # `_envelope`), so including them here is what lets an authored
            # mutation -- a handoff queued, a schedule created, a project
            # marked dirty -- reach the stream on its own, rather than only
            # ever surfacing on the next `generation` bump from a periodic
            # reindex. `git`/`burn` per card are deliberately NOT here:
            # `live_patch()` strips both from every card, so comparing them
            # against a full-update payload would spuriously fire on every
            # tick.
            return {
                "topbar": {
                    "running": payload["topbar"]["running"],
                    "queued": payload["topbar"]["queued"],
                    "scheduled": payload["topbar"]["scheduled"],
                    "dirty": payload["topbar"]["dirty"],
                    "attention": payload["topbar"]["attention"],
                },
                "diagnostics": payload["diagnostics"],
                "freshness": payload["freshness"],
                "cards": {
                    project_id: {"live": card["live"]}
                    for project_id, card in payload["cards"].items()
                },
                "unattributed": payload["unattributed"],
                "refresh": {"error": payload["refresh"]["error"]},
            }

        def stream():
            started = time.monotonic()
            ticks = 0
            previous = None
            previous_generation = None
            previous_live_signature = None
            since = app.state.notifier.revision   # captured BEFORE the first build
            try:
                while True:
                    status = refresh_coordinator.status_snapshot()
                    built_at = time.monotonic()
                    if previous is None:
                        payload = dashboard_builder.full_update()
                        yield _frame("snapshot", payload)
                        previous_generation = payload["generation"]
                        previous = payload
                        previous_live_signature = live_signature(payload)
                    elif status.generation != previous_generation:
                        payload = dashboard_builder.full_update()
                        yield _frame("update", payload)
                        previous_generation = payload["generation"]
                        previous = payload
                        previous_live_signature = live_signature(payload)
                    else:
                        payload = dashboard_builder.live_patch()
                        current_live_signature = live_signature(payload)
                        if current_live_signature != previous_live_signature:
                            yield _frame("update", payload)
                        previous = payload
                        previous_live_signature = current_live_signature
                    ticks += 1

                    if max_ticks is not None and ticks >= max_ticks:
                        break
                    if time.monotonic() - started >= max_seconds:
                        # Cap the stream and tell the client to resync rather
                        # than running an unbounded generator. EventSource
                        # reconnects on its own and gets a fresh snapshot.
                        yield _frame("refresh", {"reason": "stream capped"})
                        break

                    # Rebuild floor: never re-probe faster than `floor`. A
                    # bump wakes `wait` early, but the remainder is slept out
                    # first so a storm of bumps still can't drive the probe
                    # cost above one rebuild per `floor` seconds.
                    elapsed = time.monotonic() - built_at
                    if elapsed < floor:
                        time.sleep(floor - elapsed)
                    # Wait for the next change (or the fallback timeout). The
                    # store lock is NOT held here.
                    since = app.state.notifier.wait(since=since, timeout=interval)
                    yield ": heartbeat\n\n"
            finally:
                with _sse_lock:
                    _sse_connections["n"] -= 1

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _diagnostics(state: AgentsState | None = None) -> dict:
        return diagnostics.collect(store, cfg, update_checker, state)

    @app.get("/api/diagnostics")
    def api_diagnostics():
        return _diagnostics()

    @app.get("/diagnostics", response_class=HTMLResponse)
    def diagnostics_view(request: Request):
        diag = _diagnostics()
        return templates.TemplateResponse(
            request, "diagnostics.html",
            {
                "diag": diag,
                "alert": diagnostics.needs_attention(diag),
                "attention": diagnostics.attention_items(diag),
                "active": "diagnostics",
                "layout": _layout_for(request),
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        # ONE probe for the whole page, threaded into both `build_cards` and
        # `build_overview` so the two can never disagree about what is
        # running. The per-project cards, compose/launch/handoff surface,
        # scheduled-runs detail, and hidden-projects panel all moved off `/`
        # in earlier milestones -- they render at `/project/{id}`,
        # `/schedule`, and `/projects` now, which is why this route no longer
        # builds any of them.
        now = now_epoch()
        probe = dashboard_builder._live_state(now)
        cards = build_cards(store, cfg, agents_fn=lambda: probe,
                            debouncer=None, hook_state=None, git_cache=git_cache)
        model = build_overview(store, cfg, live_state=probe, cards=cards, now=now,
                               git_cache=git_cache)
        return templates.TemplateResponse(
            request,
            "overview.html",
            {
                "model": model,
                # `model.diagnostics_alert` IS `diagnostics.needs_attention(diag)` for
                # this same probe -- both read the same three conditions off
                # the same snapshot, so reusing it here (rather than calling
                # `_diagnostics`/`_needs_attention` a second time) cannot let
                # the header disagree with the model it was computed from.
                "diag_alert": model.diagnostics_alert,
                "active": "overview",
                "layout": _layout_for(request),
            },
        )

    @app.get("/projects", response_class=HTMLResponse)
    def projects_view(request: Request):
        model = build_projects(store, cfg, git_cache=git_cache)
        return templates.TemplateResponse(
            request,
            "projects.html",
            {
                "rows": model.rows,
                "counts": model.counts,
                "hidden": model.hidden,
                "active": "projects",
                "layout": _layout_for(request),
            },
        )

    @app.get("/project/{project_id}", response_class=HTMLResponse)
    def detail(
        request: Request, project_id: int, tab: str = "current",
        page: int = Query(0, ge=0),
        sort: str | None = None,
        direction: str | None = Query(None, alias="dir"),
        filter_value: str | None = Query(None, alias="filter"),
    ):
        # One live probe for the whole page view, shared between the workspace
        # model and the cross-project token total below -- the same "probe
        # once per view" rule the dashboard route already follows.
        #
        # `sort`/`dir`/`filter` drive the history tables' P2 controls;
        # `build_workspace` normalizes each against the selected tab's whitelist
        # and facet set (an unknown value falls back to the default), the same
        # "unknown -> default" contract `tab`/`page` already follow, so a
        # hand-typed or hostile value never 400s or reaches the SQL raw.
        now = now_epoch()
        probe = dashboard_builder._live_state(now)
        model = build_workspace(store, cfg, project_id, tab, page=page,
                                sort=sort, direction=direction,
                                filter_value=filter_value,
                                live_state=probe, git_cache=git_cache)
        if model is None:
            raise HTTPException(status_code=404, detail="unknown project")
        return templates.TemplateResponse(
            request,
            "project.html",
            {
                "model": model,
                "active": "projects",
                # The workspace is a swap target: with the fragment header the
                # router swaps it (and its tabs/sort/filter) into the persistent
                # shell instead of tearing the shell down on every click.
                "layout": _layout_for(request),
                # The schedule mini-form's hint line reads the same
                # across-every-project total the dashboard's own compose box
                # shows -- summed directly off `store.token_totals`, the exact
                # read `build_cards` already does per project, rather than
                # building every card again just to add `tokens_5h` back up.
                "totals": {
                    "last_5h": sum(
                        store.token_totals(p["id"], now - FIVE_HOURS)
                        for p in store.projects()
                    ),
                },
            },
        )

    @app.get("/schedule", response_class=HTMLResponse)
    def schedule_view_route(
        request: Request, view: str = "upcoming", page: int = Query(0, ge=0),
        status: str | None = None,
    ):
        # An unrecognized `view` (or `status`) never 400s or blanks the page --
        # `build_schedule` itself normalizes both, the same "unknown
        # tab/view/filter -> default" contract every other route in the
        # redesign follows.
        model = build_schedule(store, view=view, page=page, status=status)
        return templates.TemplateResponse(
            request,
            "schedule.html",
            {
                "model": model,
                "active": "schedule",
                "layout": _layout_for(request),
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_route(request: Request):
        # Read-only page (spec: no new write API) -- `build_settings` only
        # reads `cfg` and, for status purposes only, the real
        # `~/.claude/settings.json`; no `settings_path` override here, so a
        # test hitting this route relies on the conftest guard to keep that
        # read off the developer's real file.
        model = build_settings(cfg)
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "model": model,
                "active": "settings",
                "layout": _layout_for(request),
            },
        )

    @app.get("/api/projects")
    def projects():
        return [dict(r) for r in store.projects()]

    @app.patch("/api/projects/{project_id}")
    def patch_project(project_id: int, body: ProjectPatch):
        """Hide a project from the dashboard, archive it, or restore it.

        The existence check is not decoration: `set_project_status` is a bare
        UPDATE with no rowcount check, so an unknown id would otherwise be a 200
        that changed nothing -- indistinguishable, at the far end of a `fetch()`,
        from a hide that worked.
        """
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="unknown project")
        if body.status is not None:
            store.set_project_status(project_id, body.status)
        if body.pinned is not None:
            store.set_project_pinned(project_id, body.pinned)
        return dict(store.get_project(project_id))

    @app.post("/api/refresh")
    def refresh():
        result = refresh_coordinator.run_once()
        return dashboard_builder.full_update(refresh=result)

    # --- handoffs and launching ----------------------------------------------
    #
    # Mounted from `routes_handoffs`: capture and consume are two halves of one
    # contract, so they share a module rather than a boundary.
    app.include_router(
        build_handoffs_router(
            store=store,
            cfg=cfg,
            launch_fn=launch_fn,
            notify=lambda: app.state.notifier.bump(),
        )
    )


    # --- scheduled runs -------------------------------------------------------
    #
    # Mounted from `routes_schedule` rather than declared here. `notify` is a
    # callable, not the notifier itself, so the `app.state.notifier` lookup
    # stays lazy exactly as it was when these handlers were inline.
    app.include_router(
        build_schedule_router(
            store=store,
            cfg=cfg,
            launch_fn=launch_fn,
            notify=lambda: app.state.notifier.bump(),
        )
    )

    # --- self-update ----------------------------------------------------------
    #
    # A local-only maintenance action, guarded by three checks stacked on top
    # of `_same_origin_writes_only` above (which already enforces the `Origin`
    # leg for every unsafe method): a per-install bearer token, `Sec-Fetch-Site`,
    # and an exact match against the SHA the checker itself is currently
    # offering. Together they are what makes this safe to expose as a same-page
    # button: no cross-site page can read the token or forge the header, and no
    # request -- however it got the token -- can install anything but the
    # concrete commit already surfaced as `behind`.

    @app.post("/api/update")
    def api_update(request: Request, payload: UpdateIn):
        # 1) Per-install bearer token. `compare_digest` avoids a timing oracle.
        expected = update.read_or_create_token()
        auth = request.headers.get("authorization", "")
        presented = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not presented or not secrets.compare_digest(presented, expected):
            log.warning("refused /api/update: bad or missing token")
            raise HTTPException(status_code=403, detail="bad update token")
        # 2) Sec-Fetch-Site: a browser sets this and a page cannot forge it.
        #    Absent (a server-side client like the CLI) stays allowed; a
        #    cross-site/same-site value is refused. (Origin is already checked
        #    by `_same_origin_writes_only` for every unsafe method.)
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            log.warning("refused /api/update: Sec-Fetch-Site=%r", site)
            raise HTTPException(status_code=403, detail="cross-site update refused")
        # 3) Install ONLY the exact SHA the check surfaced -- never a re-resolved
        #    @main. A mismatch means the panel's offer and the request disagree.
        snap = request.app.state.update_checker.snapshot()
        if snap.state != "behind" or payload.target_sha != snap.latest_sha:
            raise HTTPException(status_code=409,
                                detail="target SHA is not the currently offered update")
        # 4) Under a managed panel LaunchAgent, install ASYNCHRONOUSLY. An
        #    in-process `run_update` reinstalls the package but leaves THIS
        #    process running the old code until something restarts it -- so the
        #    panel would keep serving the superseded build. `bootstrap_updater`
        #    spawns the detached one-shot job that installs AND restarts the
        #    panel; we answer 202 immediately and the banner's reconnect reads
        #    the update-state file once the panel comes back. A manual `bridge
        #    serve` has no agent to relaunch it, so it keeps the synchronous
        #    in-process path and returns the UpdateResult JSON as before.
        if update.is_managed_launchagent():
            if not setup.bootstrap_updater(payload.target_sha):
                return JSONResponse(
                    {"ok": False,
                     "error": "could not start the background updater; "
                              "run `bridge update` to retry"},
                    status_code=500)
            return JSONResponse(
                {"accepted": True, "target_sha": payload.target_sha},
                status_code=202)
        return asdict(update.run_update(payload.target_sha))

    return app
