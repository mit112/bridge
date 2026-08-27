"""The Jinja filters and globals every render surface shares.

`create_app` registers these onto its own environment; the tests that render a
single template against a bare `Environment` register the same set, so a filter
added here reaches every surface at once. Kept out of `api` so a caller that
only formats a value -- `overview`, `schedule_view` -- does not have to import
the route module to reach it.
"""

from datetime import datetime, timezone

from bridge.cards import spark_points


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
    # Imported here rather than at module scope: `projects_view` imports
    # `overview`, and `overview` needs `_schedule_time_fields` from this
    # module, so a top-level import would put the cycle back -- which is the
    # thing that used to force `overview` and `schedule_view` into lazy
    # imports on a per-row render path. One deferred import in setup code that
    # runs once beats two on a hot loop.
    from bridge.projects_view import group_projects, status_label

    env.filters["ago"] = _ago
    env.filters["ago_epoch"] = _ago_epoch
    env.filters["kilo"] = _kilo
    env.filters["spark_points"] = spark_points
    env.filters["group_projects"] = group_projects
    env.filters["status_label"] = status_label
    env.globals["shell_freshness"] = lambda: {
        "server": "available", "index_at": None, "index_age_seconds": None,
    }
    env.globals["update_token"] = lambda: ""
