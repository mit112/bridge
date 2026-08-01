"""FastAPI application. Read-only in Phase 1; Phase 2 adds handoff capture.

This process stays the sole writer. The `bridge` CLI speaks HTTP to it and never
opens the database. Phase 3 makes it the sole *spawner* too: the card and the CLI
are both thin clients of `POST /api/launch`, and neither imports `launcher`.
"""

import json
import time
from collections.abc import Callable
from dataclasses import asdict
from dataclasses import replace as dataclasses_replace
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator, model_validator

from bridge import agents, hooks, launcher, spool
from bridge.cards import FIVE_HOURS, LivenessDebouncer, build_cards, spark_points
from bridge.config import Config
from bridge.indexer import reindex
from bridge.models import Handoff
from bridge.registry import display_name, resolve_project
from bridge.store import Store, now_epoch

HERE = Path(__file__).parent

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
    templates.env.filters["spark_points"] = spark_points
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # One debouncer for the whole app, because the busy -> idle hold is state
    # ACROSS requests: a per-request instance would have nothing to remember
    # and the hysteresis would never fire.
    debouncer = LivenessDebouncer()

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

    def _frame(event: str, payload: dict) -> str:
        # The trailing BLANK line terminates the frame. With a single "\n" the
        # browser buffers forever and no event ever fires, with no error
        # anywhere -- which is why the tests assert on it explicitly.
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

    def _live_snapshot() -> dict:
        """Live state keyed by project path. Acquires the store lock only for
        the two cheap reads it needs, and never holds it across a sleep."""
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
               max_seconds: float = SSE_MAX_SECONDS):
        def stream():
            started = time.monotonic()
            ticks = 0
            previous = None
            while True:
                payload = _live_snapshot()   # takes and releases the lock
                if previous is None:
                    yield _frame("snapshot", payload)
                else:
                    delta = _delta(previous, payload)
                    if delta is not None:
                        yield _frame("delta", delta)
                previous = payload
                ticks += 1

                if max_ticks is not None and ticks >= max_ticks:
                    break
                if time.monotonic() - started >= max_seconds:
                    # Cap the stream and tell the client to resync rather than
                    # running an unbounded generator. EventSource reconnects on
                    # its own and gets a fresh snapshot.
                    yield _frame("refresh", {"reason": "stream capped"})
                    break
                time.sleep(interval)         # the lock is NOT held here
                yield ": heartbeat\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _diagnostics() -> dict:
        """Everything the diagnostics view shows, as plain data.

        Shared by the JSON route and the HTML one so the two can never disagree
        about what is wrong.
        """
        run = store.latest_index_run()
        last_index = dict(run) if run is not None else None
        # A fresh install has no runs at all; the route must answer, not 500.
        parse_errors = int((last_index or {}).get("parse_errors") or 0)

        live = agents.probe()
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

    def _needs_attention(diag: dict) -> bool:
        """A permanent "diagnostics" link would train the eye to ignore it."""
        return bool(diag["parse_errors"] or diag["spool_depth"]
                    or diag["live"] == "unavailable")

    @app.get("/api/diagnostics")
    def api_diagnostics():
        return _diagnostics()

    @app.get("/diagnostics", response_class=HTMLResponse)
    def diagnostics_view(request: Request):
        diag = _diagnostics()
        return templates.TemplateResponse(
            request, "diagnostics.html",
            {"diag": diag, "alert": _needs_attention(diag)},
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        cards = build_cards(store, cfg, debouncer=debouncer,
                            hook_state=hook_state)
        # `store.projects()` whitelists `active`, so a hidden project is absent
        # from the cards entirely. Without this list, hiding one would be a
        # one-way door: nothing in the panel could name it again, let alone
        # restore it.
        hidden = [
            r for r in store.projects(include_hidden=True) if r["status"] != "active"
        ]
        # Called once, not twice: `_diagnostics()` runs the liveness sensor, and
        # the topbar's running count has to be the same number the alert beside
        # it was computed from.
        diag = _diagnostics()
        # Built from the same snapshot the SSE stream sends, so the block below
        # and the live ticks that patch it can never disagree about what is
        # running where -- the same reason the overlay is shared at line 218.
        snapshot = _live_snapshot()
        unattributed = [
            dict(snapshot["live"][cwd], cwd=cwd) for cwd in snapshot["unattributed"]
        ]
        last_5h = sum(c.tokens_5h for c in cards)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "cards": cards,
                "hidden": hidden,
                "unattributed": unattributed,
                "diag_alert": _needs_attention(diag),
                "totals": {
                    "today": sum(c.tokens_today for c in cards),
                    "last_5h": last_5h,
                    # A rate over a measured window, not a share of a cap. The
                    # 5h plan publishes no total, so a percentage would have no
                    # denominator -- this divides by the window's own length,
                    # read from the constant that defines it so the two cannot
                    # drift apart. Integer division: the figure is rendered to
                    # the nearest thousand anyway.
                    "burn_rate": last_5h // (FIVE_HOURS // 3600),
                    "projects": len(cards),
                    "running": diag["running_sessions"],
                    "queued": diag["queued_handoffs"],
                    "last_index": (diag["last_index"] or {}).get("ran_at"),
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
                # Task 7 added the launch-history table to the template but
                # nothing ever passed it, so the block was inert in the live app.
                "launches": store.launches(project_id),
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
            permission_mode=body.permission_mode,
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
