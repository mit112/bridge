"""FastAPI application. Read-only in Phase 1; Phase 2 adds handoff capture.

This process stays the sole writer. The `bridge` CLI speaks HTTP to it and never
opens the database. Phase 3 makes it the sole *spawner* too: the card and the CLI
are both thin clients of `POST /api/launch`, and neither imports `launcher`.
"""

import dataclasses
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from dataclasses import replace as dataclasses_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from bridge import agents, hooks, launcher, schedspool, spool
from bridge.cards import (
    FIVE_HOURS,
    GitProbeCache,
    LivenessDebouncer,
    build_cards,
    spark_points,
)
from bridge.config import Config
from bridge.dashboard import DashboardBuilder
from bridge.models import AgentsState, Handoff, ScheduledRun
from bridge.notify import ChangeNotifier
from bridge.overview import build_overview
from bridge.projects_view import build_projects, group_projects, status_label
from bridge.refresh import RefreshCoordinator
from bridge.registry import display_name, resolve_project
from bridge.schedule_view import build_schedule
from bridge.settings_view import build_settings
from bridge.store import Store, now_epoch
from bridge.workspace import build_workspace

HERE = Path(__file__).parent

log = logging.getLogger(__name__)

HandoffStatus = Literal["queued", "consumed", "dismissed", "superseded"]

# Three values, not two. `archived` is what `config.toml` seeds for a directory
# that is gone; `hidden` is what the panel's own control writes; `active`
# restores either. They filter identically in `Store.projects`, which whitelists
# `active` -- the distinction is a record of who decided, and that is what makes
# the seed-versus-override rule in `indexer.reindex` legible.
ProjectStatus = Literal["active", "hidden", "archived"]

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
    # Absent means "ask as usual". Deliberately has no server-side memory: the
    # panel re-sends it per launch, so a dangerous mode can never carry over.
    permission_mode: str | None = None

    @field_validator("permission_mode")
    @classmethod
    def _known_permission_mode(cls, value: str | None) -> str | None:
        # Rejected at the edge with a 422 rather than deep inside `launch()`,
        # and read from `launcher.PERMISSION_MODES` rather than restated so the
        # vocabulary has one source. "" is the select's default and means none.
        if value and value not in launcher.PERMISSION_MODES:
            raise ValueError(
                f"permission_mode must be one of "
                f"{sorted(launcher.PERMISSION_MODES)}"
            )
        return value

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        # Validated here rather than left to `launch()` so the check does not
        # depend on which launcher is injected, and consumed from `launcher.MODES`
        # rather than restated, so the vocabulary has one source.
        if value not in launcher.MODES:
            raise ValueError(f"mode must be one of {launcher.MODES}")
        return value


def _validate_prompt_field(value: str | None) -> str | None:
    """Shared by `ScheduleIn` and `SchedulePatch`: map `launcher.LaunchError`
    to a Pydantic `ValueError`, so a NUL or oversize prompt is a 422 at the
    edge instead of surfacing later, mid-fire, as an uncaught exception."""
    if value is not None:
        try:
            launcher.validate_prompt(value)
        except launcher.LaunchError as exc:
            raise ValueError(str(exc)) from exc
    return value


def _check_known_mode(value: str) -> str:
    """Shared by `ScheduleIn` and `SchedulePatch`. Checks against the same
    closed set `LaunchIn._known_mode` does, written once here rather than
    copied into both, which is also what keeps the mutation harness's anchor
    into `LaunchIn`'s own check (`tools/mutations/phase3-task4.json`) matching
    exactly once."""
    if value not in launcher.MODES:
        raise ValueError(f"mode must be one of {launcher.MODES}")
    return value


def _check_known_permission_mode(value: str | None) -> str | None:
    """Shared by `ScheduleIn` and `SchedulePatch`; see `_check_known_mode`."""
    if value and value not in launcher.PERMISSION_MODES:
        raise ValueError(
            f"permission_mode must be one of {sorted(launcher.PERMISSION_MODES)}"
        )
    return value


class ScheduleIn(BaseModel):
    """A session to launch at a future time. Mirrors `LaunchIn`'s validators:
    `mode` and `permission_mode` are checked against the same closed sets, and
    `prompt` -- required here, unlike `LaunchIn`, since a scheduled run has no
    running request to fall back to a queued handoff from -- runs through the
    same `validate_prompt` a manual launch would hit at fire time, so a
    doomed-to-fail prompt is refused at scheduling instead of at 3am.
    """

    project_path: str
    prompt: str
    scheduled_for: int
    mode: str = "terminal"
    model: str | None = None
    effort: str | None = None
    summary: str | None = None
    permission_mode: str | None = None
    source_handoff_id: str | None = None

    @field_validator("permission_mode")
    @classmethod
    def _known_permission_mode(cls, value: str | None) -> str | None:
        return _check_known_permission_mode(value)

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        return _check_known_mode(value)

    @field_validator("prompt")
    @classmethod
    def _valid_prompt(cls, value: str) -> str:
        return _validate_prompt_field(value)

    @field_validator("scheduled_for")
    @classmethod
    def _sane_epoch(cls, value: int) -> int:
        # Rejects anything outside year-0-to-3000: a value this far outside
        # any real schedule is almost certainly a unit mistake (ms instead of
        # seconds) rather than an intentional far-future run, and letting it
        # through would only surface later as a `datetime.fromtimestamp`
        # crash in the dashboard render.
        if value < 0 or value > 32_503_680_000:
            raise ValueError("scheduled_for must be a sane epoch-seconds value")
        return value


class SchedulePatch(BaseModel):
    """Edits a still-`pending` scheduled run. Every field is optional -- a
    caller edits only what changed -- but `store.edit_pending` turns an empty
    set of fields into an empty `SET` clause, so an empty body is rejected
    the same way `HandoffPatch` and `ProjectPatch` reject theirs.
    """

    prompt: str | None = None
    scheduled_for: int | None = None
    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    summary: str | None = None
    permission_mode: str | None = None

    @field_validator("permission_mode")
    @classmethod
    def _known_permission_mode(cls, value: str | None) -> str | None:
        return _check_known_permission_mode(value)

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str | None) -> str | None:
        return value if value is None else _check_known_mode(value)

    @field_validator("prompt")
    @classmethod
    def _valid_prompt(cls, value: str | None) -> str | None:
        return _validate_prompt_field(value)

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("supply at least one field")
        return self

    @model_validator(mode="after")
    def _no_explicit_null_on_required_fields(self):
        # `exclude_unset=True` at the route is what makes an OMITTED field a
        # no-op -- but it cannot tell an omitted field from an EXPLICIT
        # `null` for the same reason: both are simply absent from
        # `model_fields_set` until pydantic sees the key at all, and an
        # explicit `null` *does* set it, with a `None` value. `prompt`,
        # `mode`, and `scheduled_for` back NOT NULL columns, so a `None` that
        # reaches `store.edit_pending` for one of them is a 500, not a no-op.
        for name in ("prompt", "mode", "scheduled_for"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class ProjectPatch(BaseModel):
    """`status`, `pinned`, or both -- but never neither.

    Both optional because the panel pins a project without touching its
    visibility and hides one without touching its pin. Optional fields alone,
    though, make `PATCH {}` a 200 that changes nothing, so the validator below
    rejects the empty body: at the far end of a `fetch()` a silent no-op is
    indistinguishable from a saved change. Same shape as `HandoffPatch`, for
    exactly the same reason.
    """

    status: ProjectStatus | None = None
    pinned: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if self.status is None and self.pinned is None:
            raise ValueError("supply status, pinned, or both")
        return self


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

    The one tail `POST /api/launch` and Task 3's scheduler both need: neither
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
    /api/schedule/{id}/run-now` and Task 4's scheduler both end here, with
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
        log.exception("failed to journal claim of scheduled run %r", id)
        store.finish_scheduled_run(
            id, status="failed", error=f"could not journal the claim: {exc}"
        )
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


class CachedStaticFiles(StaticFiles):
    """`StaticFiles` that answers with a `Cache-Control`, which it otherwise omits.

    Starlette sends only `etag`/`last-modified`, which gives the browser no
    freshness lifetime at all -- so every navigation re-requests the
    render-blocking 95KB `/static/app.css` AND all six woff2 faces just to be
    told 304. Seven conditional round trips in front of first paint, for bytes
    already on disk.

    The max-ages are deliberately SHORT because these URLs are unversioned and
    Bridge is a local panel whose only user edits this CSS by hand. `immutable`
    or a multi-day age would mean an `app.css` edit stops appearing until a hard
    reload -- exactly the trap already recorded against this repo ("browsers
    cache app.css even though the server re-reads it"). A minute covers a burst
    of clicks through the nav and expires well inside an edit-and-reload cycle;
    `must-revalidate` forbids ever serving it stale past that.

    Fonts get a day: their content genuinely never changes -- a different weight
    is a different filename, so a stale hit is impossible rather than merely
    unlikely. Still revalidatable, not `immutable`, for the same reason as
    above: nothing here is worth a cache that cannot be cleared by a reload.
    """

    ASSET_CACHE_CONTROL = "public, max-age=60, must-revalidate"
    FONT_CACHE_CONTROL = "public, max-age=86400, must-revalidate"

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        # Set after `super()` so the 304 branch is covered too: `cache-control`
        # is one of the headers Starlette carries onto a `NotModifiedResponse`,
        # and a revalidation that answered without one would re-arm the same
        # header-less loop on the very next navigation.
        response.headers["cache-control"] = (
            self.FONT_CACHE_CONTROL
            if Path(full_path).suffix == ".woff2"
            else self.ASSET_CACHE_CONTROL
        )
        return response


FRAGMENT_HEADER = "x-bridge-fragment"

# The methods a cross-origin form post can reach with side effects. GET and
# HEAD are excluded because every one of Bridge's is a read.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _layout_for(request: Request) -> str:
    """Which layout a page template extends.

    A request without the header renders exactly what it always did, which is
    what keeps the existing route tests a true statement about the app.
    """
    return "_fragment.html" if request.headers.get(FRAGMENT_HEADER) else "base.html"


def create_app(
    store: Store, cfg: Config, launch_fn: LaunchFn = launcher.launch,
    refresh_coordinator: RefreshCoordinator | None = None,
    notifier: ChangeNotifier | None = None,
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

    def _live_snapshot(state: AgentsState | None = None) -> dict:
        """Live state keyed by project path. Acquires the store lock only for
        the two cheap reads it needs, and never holds it across a sleep.

        `state` lets a caller that has already probed reuse the result. The
        dashboard needs liveness three times over -- the cards, the diagnostics
        alert and the unattributed block -- and three separate probes would
        observe three different instants, putting three disagreeing pictures of
        what is running on one page.
        """
        if state is None:
            state = agents.probe()
        # Same overlay the dashboard applies, so a card and a live tick can
        # never disagree about whether a session is waiting on a human.
        if state.status == "ok":
            hook_state.forget(s.session_id for s in state.sessions)
            waiting = hook_state.waiting_ids()
            if waiting:
                state = dataclasses_replace(state, sessions=[
                    dataclasses_replace(s, status=hooks.NEEDS_INPUT)
                    if s.session_id in waiting else s for s in state.sessions
                ])
        # The same hysteresis `build_cards` applies, in the same order relative
        # to the hook overlay, from the SAME debouncer instance. Both consumers
        # have to run it or neither is debounced in practice: the render would
        # hold a card at "busy" while the SSE tick told the client "idle", and a
        # flap the debouncer exists to swallow would reach the wire anyway.
        # `apply` is idempotent for one `now`, so the dashboard calling this and
        # `build_cards` off one probe settles both to the same answer.
        state = dataclasses_replace(
            state, sessions=debouncer.apply(state.sessions, now_epoch())
        )
        rows = store.projects()
        grouped = agents.by_project(
            state, store.alias_map(), [row["path"] for row in rows]
        )
        live = {}
        unattributed = []
        for path, sessions in grouped.items():
            if not sessions:
                continue
            if path == agents.UNATTRIBUTED:
                # Keyed by their own cwd rather than skipped. `agents.py:300`
                # says these must not be lost, and both consumers were losing
                # them anyway: this loop dropped the bucket and `build_cards`
                # only ever looks up exact project paths. The topbar's running
                # count does include them, so dropping them here made the count
                # and the cards disagree with nothing on the page to explain it.
                # `by_project` sorted most-recent-first, so the first cwd wins.
                for s in sessions:
                    if s.cwd in live:
                        continue
                    live[s.cwd] = {"status": s.status,
                                   "started_at": s.started_at}
                    unattributed.append(s.cwd)
                continue
            session = sessions[0]
            live[path] = {"status": session.status,
                          "started_at": session.started_at}
        run = store.latest_index_run()
        return {
            "live": live,
            # Which of `live`'s keys are directories rather than projects. Only
            # the server render needs this: `_delta` diffs `live`, which already
            # carries their appearance and disappearance.
            "unattributed": unattributed,
            "unavailable": state.status == "unavailable",
            "index": {"ran_at": run["ran_at"],
                      "parse_errors": run["parse_errors"]} if run else None,
        }

    def _delta(before: dict, after: dict) -> dict | None:
        """What changed, including what is GONE.

        The plan's payload shape could say "busy" but had no way to say "this
        session has ended", so a card kept its live band until the page was
        reloaded. `removed` is that missing word.
        """
        changed = {p: v for p, v in after["live"].items()
                   if before["live"].get(p) != v}
        removed = [p for p in before["live"] if p not in after["live"]]
        if not changed and not removed and before["index"] == after["index"] \
                and before["unavailable"] == after["unavailable"]:
            return None  # emit only on change
        return {"live": changed, "removed": removed,
                "unavailable": after["unavailable"], "index": after["index"]}

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
            return {
                "topbar": {"running": payload["topbar"]["running"]},
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
        """Everything the diagnostics view shows, as plain data.

        Shared by the JSON route and the HTML one so the two can never disagree
        about what is wrong.
        """
        run = store.latest_index_run()
        last_index = dict(run) if run is not None else None
        # A fresh install has no runs at all; the route must answer, not 500.
        parse_errors = int((last_index or {}).get("parse_errors") or 0)

        live = agents.probe() if state is None else state
        return {
            "last_index": last_index,
            "parse_errors": parse_errors,
            "spool_depth": spool.pending_count(cfg.spool_dir),
            "live": live.status,
            "running_sessions": sum(
                1 for s in live.sessions if not agents.is_terminal(s.status)
            ),
            # Recorded so a future schema drift is a diagnosis rather than a
            # bisect: which sensor answered, and what version it reported.
            "live_source": live.source,
            "claude_version": live.version,
            "queued_handoffs": store.queued_handoff_count(),
        }

    def _attention_items(diag: dict) -> list[dict]:
        """The one place that decides which checks are failing/degraded, and
        what to say about each. Turns each into plain language: what it means
        and what to do about it. Presented under "Needs attention" so a fresh
        install is not led anywhere -- this is display-only grouping of the
        same `_diagnostics()` dict, never a new probe.

        `_needs_attention` below defers to this list's truthiness rather than
        re-checking the same three conditions itself: two independent copies
        of "what counts as degraded" would let the top-of-page banner and
        this section silently disagree the next time a condition is added to
        only one of them.
        """
        items = []
        if diag["parse_errors"]:
            items.append({
                "label": "Parse errors during indexing",
                "cause": f"{diag['parse_errors']} line(s) in session files "
                         "failed to parse during the last index run.",
                "next_action": "Re-run indexing (POST /api/refresh) and check "
                         "the Bridge server log for the file and line that "
                         "failed; malformed JSONL lines are skipped, not fatal.",
            })
        if diag["spool_depth"]:
            items.append({
                "label": "Handoffs stuck in the spool",
                "cause": f"{diag['spool_depth']} handoff file(s) are queued "
                         "in the spool directory and have not been drained.",
                "next_action": "Confirm the spool drain process is running; "
                         "files remain in spool_dir until Bridge successfully "
                         "drains them.",
            })
        if diag["live"] == "unavailable":
            items.append({
                "label": "Liveness sensor unavailable",
                "cause": f"The {diag['live_source']} sensor could not "
                         "determine which Claude sessions are running.",
                "next_action": "Check that Claude Code's session registry "
                         "(or subprocess probe) is reachable on this machine, "
                         "then reload Diagnostics.",
            })
        return items

    def _needs_attention(diag: dict) -> bool:
        """A permanent "diagnostics" link would train the eye to ignore it."""
        return bool(_attention_items(diag))

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
                "alert": _needs_attention(diag),
                "attention": _attention_items(diag),
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
                # `model.diagnostics_alert` IS `_needs_attention(diag)` for
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

    @app.get("/api/handoffs")
    def list_handoffs_by_path(project_path: str):
        """Every queued handoff for a project, for the panel's stacked view.
        Returns [] (not 204) for an unknown or empty project, so the client
        renders 'nothing queued' without special-casing a no-content status."""
        canonical = store.alias_map().get(project_path, project_path)
        project = store.project_by_path(canonical)
        if project is None:
            return []
        return [dict(r) for r in store.queued_handoffs(project["id"])]

    @app.get("/api/handoffs/{project_id}")
    def list_handoffs(project_id: int):
        return [dict(r) for r in store.queued_handoffs(project_id)]

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

    # --- scheduled runs -------------------------------------------------------
    #
    # A schedule is created, listed, edited, cancelled, or fired early -- but
    # only ever fired FOR REAL by Task 4's background scheduler. `run-now`
    # exists so the panel can test a schedule (or just stop waiting for it)
    # without a second code path to keep in sync with the scheduler's own.

    @app.post("/api/schedule", status_code=201)
    def post_schedule(body: ScheduleIn):
        # Mirrors `post_launch`'s check for `body.handoff_id`: without it a
        # made-up id only fails at fire time, deep inside `_fire_claimed_job`,
        # as a foreign-key error instead of a 404 at the edge.
        if body.source_handoff_id is not None:
            if store.get_handoff(body.source_handoff_id) is None:
                raise HTTPException(status_code=404, detail="unknown handoff")
        project_path = store.alias_map().get(body.project_path, body.project_path)
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
        return {**dict(store.get_scheduled_run(job.id)), "journaled": journaled}

    @app.get("/api/schedule")
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

    @app.patch("/api/schedule/{id}")
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
        return dict(store.get_scheduled_run(id))

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

    @app.post("/api/schedule/{id}/run-now")
    def run_now(id: str):
        row = store.claim_specific(id)
        if row is None:
            raise _unknown_or_conflict(id)
        return dict(_fire_claimed_job(store, cfg, row, launch_fn))

    @app.post("/api/schedule/{id}/retry")
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
            return dict(store.get_scheduled_run(row["id"]))
        return dict(_fire_claimed_job(store, cfg, row, launch_fn))

    return app


def _schedule_time_fields(epoch: int) -> tuple[str | None, str]:
    """The dashboard's UTC fallback for `job.scheduled_for`, guarded against a
    row that predates `ScheduleIn`'s epoch-seconds bound. `ScheduleIn` refuses
    an out-of-range value at creation, but a row seeded before that check
    existed (or straight through the store, bypassing the API) can still
    carry one, and `datetime.fromtimestamp` raises rather than clamping --
    which must degrade this one row's display, not 500 the whole page.
    """
    try:
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None, str(epoch)
    return dt.isoformat(), dt.strftime("%Y-%m-%d %H:%M UTC")


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


def register_template_filters(env) -> None:
    """Register every Jinja filter Bridge's templates use, onto any env.

    `create_app` calls this for its own `Jinja2Templates` env; the handful of
    tests that build a bare `Environment` to render one template in isolation
    call it too, so their filter set can never drift from the app's -- add a
    filter here once and every render surface has it.

    `shell_freshness` rides along for the same reason. It is a global rather
    than a filter, but it is the one thing besides the filters that EVERY page
    template needs -- `base.html`'s shell readout calls it, and so does the one
    page that overrides that block -- so an env that can render a template's
    filters but not its shell is not actually able to render the template.
    The stub answers "no index run yet"; `create_app` replaces it immediately
    below its own call with the coordinator-backed one.
    """
    env.filters["ago"] = _ago
    env.filters["ago_epoch"] = _ago_epoch
    env.filters["kilo"] = _kilo
    env.filters["spark_points"] = spark_points
    env.filters["group_projects"] = group_projects
    env.filters["status_label"] = status_label
    env.globals["shell_freshness"] = lambda: {
        "server": "available", "index_at": None, "index_age_seconds": None,
    }
