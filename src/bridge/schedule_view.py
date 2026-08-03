"""Schedule read model: the full Upcoming/History surface for `/schedule`.

Assembled from a single `store.scheduled_runs()` fetch (no new SQL) plus the
same alias-map + `display_name` project-name resolution `api.py`'s dashboard
route already uses for its scheduled panel. Mirrors that route's `retried`/
`retryable`/active/terminal vocabulary exactly (see `api.py`'s `dashboard`
route, ~lines 754-792) so a run's status here never disagrees with what the
same row shows elsewhere in the panel.

History pagination is the one place in the codebase allowed to page: rather
than lean on `store.scheduled_runs(limit=..., offset=...)` -- whose SQL only
orders active-first-then-`scheduled_for` and cannot express "terminal rows,
newest-completed-first" -- this fetches the full row set once (exactly as
`api.py`'s dashboard route already does for its own terminal preview) and
paginates the sorted, filtered Python list. That keeps the ordering correct
without adding any SQL beyond `store.scheduled_runs()`'s existing no-arg call.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from bridge.overview import ScheduleRow
from bridge.registry import display_name
from bridge.store import Store

VALID_VIEWS = ("upcoming", "history")

# A scheduled run still owed to the user -- mirrors api.py's dashboard route
# own `active = [r for r in scheduled_rows if r["status"] in (...)]`.
ACTIVE_STATUSES = ("pending", "launching")

# Terminal, non-cancelled statuses that still need a human: the same set
# overview.py's SCHEDULE_FAILURE_STATUSES names for the Overview ladder, and
# the same set api.py's dashboard route grants a Retry action to.
ATTENTION_STATUSES = ("failed", "indeterminate", "missed")


@dataclass(frozen=True)
class ScheduleModel:
    """The `/schedule` page's full state for one of its two views.

    Upcoming populates `attention`/`pending`/`launching` and leaves `history`
    empty; History populates `history`/`history_total`/`page` and leaves the
    other three empty. A caller never has to guess which fields matter --
    `view` says which, and the unused lists are simply `[]` rather than None.
    """

    view: str
    attention: list[ScheduleRow] = field(default_factory=list)
    pending: list[ScheduleRow] = field(default_factory=list)
    launching: list[ScheduleRow] = field(default_factory=list)
    history: list[ScheduleRow] = field(default_factory=list)
    history_total: int = 0
    page: int = 0
    page_size: int = 25


def build_schedule(
    store: Store,
    *,
    view: str = "upcoming",
    page: int = 0,
    page_size: int = 25,
) -> ScheduleModel:
    """Assemble the Schedule page for one view.

    An unrecognized (or missing/empty) `view` silently normalizes to
    "upcoming" rather than raising -- the same "unknown tab/view -> default"
    contract every other route in the redesign follows.
    """
    normalized_view = view if view in VALID_VIEWS else "upcoming"

    rows = store.scheduled_runs()
    # Read once, outside any per-row loop -- `alias_map()`'s own contract
    # (honoured already by `api.py`'s dashboard route and `overview.py`'s
    # `build_overview`) is "read once per index run", not once per row.
    alias = store.alias_map()
    # A row already superseded by a retry must not re-offer attention or a
    # second Retry -- the same set api.py's dashboard route computes before
    # deciding what may still show a Retry control.
    retried = {r["retry_of"] for r in rows if r["retry_of"]}

    if normalized_view == "history":
        terminal = [r for r in rows if r["status"] not in ACTIVE_STATUSES]
        terminal.sort(key=lambda r: (r["completed_at"] or 0), reverse=True)
        start = page * page_size
        page_rows = terminal[start:start + page_size]
        return ScheduleModel(
            view="history",
            history=[_schedule_row(store, r, alias, retried) for r in page_rows],
            history_total=len(terminal),
            page=page,
            page_size=page_size,
        )

    attention_rows = [
        r for r in rows
        if r["status"] in ATTENTION_STATUSES and r["id"] not in retried
    ]
    attention_rows.sort(key=lambda r: (r["completed_at"] or 0), reverse=True)

    pending_rows = sorted(
        (r for r in rows if r["status"] == "pending"),
        key=lambda r: r["scheduled_for"],
    )
    launching_rows = [r for r in rows if r["status"] == "launching"]

    return ScheduleModel(
        view="upcoming",
        attention=[_schedule_row(store, r, alias, retried) for r in attention_rows],
        pending=[_schedule_row(store, r, alias, retried) for r in pending_rows],
        launching=[_schedule_row(store, r, alias, retried) for r in launching_rows],
    )


def _schedule_row(
    store: Store, row: sqlite3.Row, alias: dict[str, str], retried: set[str]
) -> ScheduleRow:
    # Imported lazily, not at module scope: `api.py` will (Task 4.2) import
    # THIS module for its `/schedule` route, so a top-level `from bridge.api
    # import ...` here would be a cycle. Mirrors the same lazy import
    # `overview._schedule_row` already uses for the identical reason.
    from bridge.api import _schedule_time_fields

    project_path = row["project_path"]
    # `project_by_path` (not the alias-resolved path) -- the same lookup
    # `overview._schedule_row` falls back to for a row whose card is gone,
    # since a scheduled run's `project_path` is stored pre-alias-resolution.
    project = store.project_by_path(project_path)
    project_id = project["id"] if project is not None else None
    project_name = display_name(alias.get(project_path, project_path))
    iso, utc = _schedule_time_fields(row["scheduled_for"])
    return ScheduleRow(
        id=row["id"],
        project_id=project_id,
        project_name=project_name,
        prompt_preview=(row["prompt"] or "")[:120],
        scheduled_for=row["scheduled_for"],
        status=row["status"],
        error=row["error"],
        scheduled_for_utc=utc,
        scheduled_for_iso=iso,
        mode=row["mode"],
        retryable=(row["status"] in ATTENTION_STATUSES and row["id"] not in retried),
    )
