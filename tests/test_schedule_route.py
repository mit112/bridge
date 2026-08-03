"""Schedule read model (Task 4.1) and the `/schedule` route (Task 4.2).

The model section (unchanged from Task 4.1) exercises `build_schedule`
directly; the route section below drives it over HTTP through `GET
/schedule`, plus the shared-macro contract between `schedule_row`'s
`interactive=False` (Overview preview) and `interactive=True` (this page)
renderings.
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from bridge.api import _ago, _ago_epoch, _kilo, create_app
from bridge.cards import spark_points
from bridge.config import load
from bridge.models import ScheduledRun
from bridge.overview import ScheduleRow as OverviewScheduleRow
from bridge.schedule_view import ScheduleModel, build_schedule
from bridge.store import Store

TPL = Path(__file__).resolve().parent.parent / "src" / "bridge" / "templates"


def _components_module():
    """Same setup `tests/test_components.py` uses to compile `_components.html`
    standalone, needed here to render `schedule_row` both ways for the
    shared-macro-contract test below without spinning up a full route."""
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters["ago"] = _ago
    env.filters["ago_epoch"] = _ago_epoch
    env.filters["kilo"] = _kilo
    env.filters["spark_points"] = spark_points
    return env.get_template("_components.html").module


def _cfg(tmp_path):
    return load({"db_path": tmp_path / "schedule.db", "spool_dir": tmp_path / "spool"})


def _run(**kwargs) -> ScheduledRun:
    defaults = dict(
        project_path="/p/proj", prompt="do the thing", mode="interactive",
    )
    defaults.update(kwargs)
    return ScheduledRun(**defaults)


def test_unknown_view_defaults_to_upcoming(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    store.upsert_project("/p/proj", "proj")

    model = build_schedule(store, view="zzz")
    assert model.view == "upcoming"

    model = build_schedule(store)
    assert model.view == "upcoming"

    model = build_schedule(store, view="history")
    assert model.view == "history"


def test_upcoming_groups_attention_then_pending_then_launching(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    store.upsert_project("/p/proj", "proj")

    # Attention: two terminal failures needing a human, newest-completed-first.
    store.restore_scheduled_run(_run(
        id="fail-old", scheduled_for=100, created_at=50,
        status="failed", completed_at=200, error="boom",
    ))
    store.restore_scheduled_run(_run(
        id="fail-new", scheduled_for=150, created_at=90,
        status="missed", completed_at=300,
    ))
    # A retried original must be excluded from attention -- only its retry
    # (still terminal-failed here) may appear.
    store.restore_scheduled_run(_run(
        id="fail-retried", scheduled_for=120, created_at=60,
        status="failed", completed_at=250, error="boom",
    ))
    store.restore_scheduled_run(_run(
        id="fail-retry-of-it", scheduled_for=400, created_at=260,
        status="indeterminate", completed_at=500, retry_of="fail-retried",
    ))
    # A cancelled run is terminal but not "needs attention".
    store.restore_scheduled_run(_run(
        id="cancelled-1", scheduled_for=130, created_at=70,
        status="cancelled", completed_at=260,
    ))

    # Pending: chronological by scheduled_for ascending.
    store.create_scheduled_run(_run(
        id="pending-later", scheduled_for=900, created_at=10,
    ))
    store.create_scheduled_run(_run(
        id="pending-earlier", scheduled_for=800, created_at=10,
    ))

    # Launching.
    store.restore_scheduled_run(_run(
        id="launching-1", scheduled_for=700, created_at=10,
        status="launching", claimed_at=690,
    ))

    model = build_schedule(store, view="upcoming")
    assert model.view == "upcoming"

    attention_ids = [row.id for row in model.attention]
    assert "fail-retried" not in attention_ids
    assert attention_ids == ["fail-retry-of-it", "fail-new", "fail-old"]
    assert "cancelled-1" not in attention_ids

    pending_ids = [row.id for row in model.pending]
    assert pending_ids == ["pending-earlier", "pending-later"]

    launching_ids = [row.id for row in model.launching]
    assert launching_ids == ["launching-1"]

    assert model.history == []


def test_retryable_flag_and_scheduled_for_utc_populated(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    store.upsert_project("/p/proj", "proj")

    store.restore_scheduled_run(_run(
        id="fail-1", scheduled_for=100, created_at=50,
        status="failed", completed_at=200, error="boom",
    ))
    store.restore_scheduled_run(_run(
        id="fail-retried", scheduled_for=120, created_at=60,
        status="failed", completed_at=250,
    ))
    store.restore_scheduled_run(_run(
        id="fail-retry-of-it", scheduled_for=400, created_at=260,
        status="indeterminate", completed_at=500, retry_of="fail-retried",
    ))
    store.restore_scheduled_run(_run(
        id="fired-1", scheduled_for=90, created_at=40,
        status="fired", completed_at=190, fired_at=190,
    ))

    model = build_schedule(store, view="upcoming")
    by_id = {row.id: row for row in model.attention}

    assert by_id["fail-1"].retryable is True
    assert by_id["fail-1"].scheduled_for_utc != ""
    assert by_id["fail-1"].mode == "interactive"
    # The retried original is excluded from attention entirely (asserted in
    # the grouping test); its retry is retryable in turn since nothing has
    # retried *it*.
    assert by_id["fail-retry-of-it"].retryable is True

    # A fired (successful) run is neither attention-worthy nor retryable.
    assert "fired-1" not in by_id


def test_history_view_paginates_terminal_rows_with_total(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    store.upsert_project("/p/proj", "proj")

    # 5 terminal rows plus 1 active (pending) row that must never appear in
    # history. completed_at spaced so newest-first ordering is unambiguous.
    for i in range(5):
        store.restore_scheduled_run(_run(
            id=f"term-{i}", scheduled_for=1000 + i, created_at=10,
            status="fired", completed_at=100 + i, fired_at=100 + i,
        ))
    store.create_scheduled_run(_run(id="still-pending", scheduled_for=2000))

    page0 = build_schedule(store, view="history", page=0, page_size=2)
    assert page0.view == "history"
    assert page0.history_total == 5
    assert page0.page == 0
    assert page0.page_size == 2
    assert len(page0.history) == 2
    # Newest-completed-first: term-4 (completed_at=104) then term-3 (103).
    assert [row.id for row in page0.history] == ["term-4", "term-3"]

    page1 = build_schedule(store, view="history", page=1, page_size=2)
    assert [row.id for row in page1.history] == ["term-2", "term-1"]
    assert page1.history_total == 5

    assert page0.attention == []
    assert page0.pending == []
    assert page0.launching == []

    # No pending/launching row ever surfaces in history.
    all_history_ids = {row.id for row in page0.history} | {row.id for row in page1.history}
    assert "still-pending" not in all_history_ids


def test_history_includes_cancelled_runs_and_they_are_not_retryable(tmp_path):
    """Locks the decided behavior: History is the full terminal record, so a
    `cancelled` run appears there (and counts toward `history_total`) even
    though it's deliberately excluded from `attention`/Overview's failure
    ladder -- cancelled means the user said not to, not that something is
    still owed. A cancelled run is also never `retryable`."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    store.upsert_project("/p/proj", "proj")

    store.restore_scheduled_run(_run(
        id="cancelled-1", scheduled_for=100, created_at=50,
        status="cancelled", completed_at=200,
    ))
    store.restore_scheduled_run(_run(
        id="fired-1", scheduled_for=90, created_at=40,
        status="fired", completed_at=190, fired_at=190,
    ))

    model = build_schedule(store, view="history", page=0, page_size=25)
    assert model.history_total == 2
    history_ids = [row.id for row in model.history]
    assert "cancelled-1" in history_ids

    cancelled_row = next(row for row in model.history if row.id == "cancelled-1")
    assert cancelled_row.retryable is False


def test_history_out_of_range_page_returns_empty_without_raising(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    store.upsert_project("/p/proj", "proj")

    store.restore_scheduled_run(_run(
        id="term-1", scheduled_for=100, created_at=50,
        status="fired", completed_at=200, fired_at=200,
    ))
    store.restore_scheduled_run(_run(
        id="term-2", scheduled_for=110, created_at=60,
        status="fired", completed_at=210, fired_at=210,
    ))

    model = build_schedule(store, view="history", page=99, page_size=25)
    assert model.history == []
    assert model.history_total == 2


def test_build_schedule_returns_frozen_model(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    model = build_schedule(store)
    assert isinstance(model, ScheduleModel)
    try:
        model.page = 5
        assert False, "ScheduleModel should be frozen"
    except AttributeError:
        pass


# --- Task 4.2: the `/schedule` route ----------------------------------------


def _client(tmp_path) -> tuple[TestClient, Store]:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    store.upsert_project("/p/proj", "proj")
    return TestClient(create_app(store, cfg)), store


def test_schedule_route_default_view_renders_groups_in_order(tmp_path):
    client, store = _client(tmp_path)
    store.restore_scheduled_run(_run(
        id="fail-1", scheduled_for=100, created_at=50,
        status="failed", completed_at=200, error="boom",
    ))
    store.create_scheduled_run(_run(id="pending-1", scheduled_for=900))
    store.restore_scheduled_run(_run(
        id="launching-1", scheduled_for=700, created_at=10,
        status="launching", claimed_at=690,
    ))

    r = client.get("/schedule")
    assert r.status_code == 200
    body = r.text
    assert body.count("<h1") == 1
    # Attention, then Pending, then Launching -- in that order in the markup.
    assert body.index("Attention") < body.index("Pending") < body.index("Launching")
    assert 'data-scheduled-job="fail-1"' in body
    assert 'data-scheduled-job="pending-1"' in body
    assert 'data-scheduled-job="launching-1"' in body


def test_schedule_route_pending_row_exposes_run_now_edit_cancel(tmp_path):
    client, store = _client(tmp_path)
    store.create_scheduled_run(_run(id="pending-1", scheduled_for=900))

    body = client.get("/schedule").text
    assert 'data-scheduled-run-now="pending-1"' in body
    assert 'data-scheduled-edit-toggle="pending-1"' in body
    assert 'data-scheduled-cancel="pending-1"' in body
    assert 'data-scheduled-edit-when="pending-1"' in body


def test_schedule_route_terminal_retryable_row_exposes_retry(tmp_path):
    client, store = _client(tmp_path)
    store.restore_scheduled_run(_run(
        id="fail-1", scheduled_for=100, created_at=50,
        status="failed", completed_at=200, error="boom",
    ))

    body = client.get("/schedule").text
    assert 'data-scheduled-retry="fail-1"' in body
    assert "data-scheduled-run-now" not in body
    assert "data-scheduled-cancel" not in body


def test_schedule_route_terminal_non_retryable_row_omits_retry(tmp_path):
    client, store = _client(tmp_path)
    # Cancelled is terminal but deliberately never retryable (the user said
    # not to, not that anything is still owed) -- surfaced only in History.
    store.restore_scheduled_run(_run(
        id="cancelled-1", scheduled_for=100, created_at=50,
        status="cancelled", completed_at=200,
    ))

    body = client.get("/schedule?view=history").text
    assert 'data-scheduled-job="cancelled-1"' in body
    assert "data-scheduled-retry=" not in body


def test_schedule_route_unknown_view_defaults_to_upcoming(tmp_path):
    client, store = _client(tmp_path)
    store.create_scheduled_run(_run(id="pending-1", scheduled_for=900))

    r = client.get("/schedule?view=zzz")
    assert r.status_code == 200
    assert "Attention" in r.text
    assert "Pending" in r.text
    assert 'data-scheduled-job="pending-1"' in r.text


def test_schedule_route_history_paginates_with_prev_next_reflecting_total(tmp_path):
    client, store = _client(tmp_path)
    # 30 terminal rows -- one more page than the 25-row default page size --
    # so page 0 and page 1 are each provably a different slice of the total.
    for i in range(30):
        store.restore_scheduled_run(_run(
            id=f"term-{i:02d}", scheduled_for=1000 + i, created_at=10,
            status="fired", completed_at=100 + i, fired_at=100 + i,
        ))

    page0 = client.get("/schedule?view=history&page=0")
    assert page0.status_code == 200
    body0 = page0.text
    assert body0.count("<h1") == 1
    ids0 = re.findall(r'data-scheduled-job="(term-\d\d)"', body0)
    assert len(ids0) == 25
    # Newest-completed-first: term-29 (completed_at=129) leads.
    assert ids0[0] == "term-29"
    assert 'href="/schedule?view=history&page=1"' in body0  # Next
    assert 'href="/schedule?view=history&page=-1"' not in body0  # no Previous
    assert "of 30" in body0

    page1 = client.get("/schedule?view=history&page=1")
    body1 = page1.text
    ids1 = re.findall(r'data-scheduled-job="(term-\d\d)"', body1)
    assert len(ids1) == 5
    assert set(ids0).isdisjoint(ids1)
    assert 'href="/schedule?view=history&page=0"' in body1  # Previous
    assert 'href="/schedule?view=history&page=2"' not in body1  # no Next (last page)
    assert "of 30" in body1


def test_history_out_of_range_page_renders_sensible_pager_text(tmp_path):
    """A hand-typed out-of-range `?page=99` returns empty rows (the model
    already clamps to nothing), and the pager must read sensibly rather than
    the old nonsensical "2476-2 of 2": the start is clamped to the total and
    an empty page reads "0 of N"."""
    client, store = _client(tmp_path)
    for i in range(2):
        store.restore_scheduled_run(_run(
            id=f"term-{i}", scheduled_for=1000 + i, created_at=10,
            status="fired", completed_at=100 + i, fired_at=100 + i,
        ))

    resp = client.get("/schedule?view=history&page=99")

    assert resp.status_code == 200
    assert "0 of 2" in resp.text
    # The unclamped start (99 * 25 = 2475, so "2476-") must not appear.
    assert "2476" not in resp.text
    # No Next link off the end, and no crash on the empty slice.
    assert 'href="/schedule?view=history&page=100"' not in resp.text


def test_shared_macro_contract_status_vocabulary_and_scheduled_for_hook_match():
    """Overview's preview (`interactive=False`) and `/schedule`'s own row
    (`interactive=True`) render the SAME `data-scheduled-for` hook and the
    SAME status word for identical row data -- so the two can never quietly
    drift into disagreeing about what a run's status is."""
    row = OverviewScheduleRow(
        id="shared-1",
        project_id=3,
        project_name="Demo",
        prompt_preview="p",
        scheduled_for=1735700000,
        status="failed",
        error="boom",
        scheduled_for_utc="2025-01-01 00:00 UTC",
        scheduled_for_iso="2025-01-01T00:00:00+00:00",
        mode="terminal",
        retryable=True,
    )
    module = _components_module()
    overview_html = module.schedule_row(row, interactive=False)
    schedule_html = module.schedule_row(row, interactive=True)

    assert 'data-scheduled-for="1735700000"' in overview_html
    assert 'data-scheduled-for="1735700000"' in schedule_html

    assert "failed" in overview_html
    assert "failed" in schedule_html
    assert "pill--failed" in overview_html
    assert "pill--failed" in schedule_html

    # Only the interactive render carries the action hooks.
    assert "data-scheduled-retry=" not in overview_html
    assert 'data-scheduled-retry="shared-1"' in schedule_html
