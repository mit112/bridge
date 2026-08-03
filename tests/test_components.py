"""Shared Jinja component macros: rendered directly (not through a route),
since `_components.html` has no route of its own -- later tasks import these
macros into `overview.html`/`project.html`/`schedule.html`.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from bridge.api import _ago, _ago_epoch, _kilo
from bridge.cards import spark_points
from bridge.overview import ProjectSummary, ScheduleRow

TPL = Path(__file__).resolve().parent.parent / "src" / "bridge" / "templates"


def _module():
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters["ago"] = _ago
    env.filters["ago_epoch"] = _ago_epoch
    # `_components.html` now also defines `live_status`/`token_burn` (Task
    # 3.3's extraction), which use `kilo`/`spark_points` -- compiling the
    # template's `.module` requires every filter it names to be registered,
    # even for a macro this test never calls.
    env.filters["kilo"] = _kilo
    env.filters["spark_points"] = spark_points
    return env.get_template("_components.html").module


def _summary(**overrides):
    fields = dict(
        project_id=7,
        name="Demo",
        path="/x/demo",
        status_word="queued",
        branch="main",
        dirty_count=3,
        last_session_title="Did work",
        last_session_age_seconds=7200,
        tokens_today=1000,
        tokens_5h=500,
        pinned=False,
    )
    fields.update(overrides)
    return ProjectSummary(**fields)


def test_project_summary_row_emits_required_hooks():
    html = _module().project_summary_row(_summary())
    assert "/project/7" in html
    assert "Open project" in html
    assert html.count("Open project") == 1
    assert "<code" in html
    assert "/x/demo" in html
    assert "queued" in html
    assert "main" in html
    assert "3 dirty" in html
    assert "Did work" in html
    assert "2h" in html  # last_session_age_seconds=7200 -> compact duration


def test_project_summary_row_clamps_negative_age_to_zero():
    # `last_session_age_seconds` should already be floored by overview.py, but
    # the macro clamps too: never render "-2m ago" if a negative value ever
    # reaches this template.
    html = _module().project_summary_row(_summary(last_session_age_seconds=-120))
    assert "0m" in html
    assert "-2m" not in html
    assert "-" not in html.split("Did work")[1].split("</span>")[0]


def test_project_summary_row_omits_dirty_when_clean():
    html = _module().project_summary_row(_summary(dirty_count=0))
    assert "dirty" not in html


def test_project_summary_row_escapes_untrusted_fields():
    html = _module().project_summary_row(
        _summary(name="<script>alert(1)</script>", path="/x/<b>demo</b>")
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_status_pill_emits_state_word_and_class():
    html = _module().status_pill("running")
    assert "running" in html
    assert 'class="pill pill--running"' in html


def test_empty_state_emits_message_and_class():
    html = _module().empty_state("No projects yet")
    assert 'class="empty"' in html
    assert "No projects yet" in html


def test_history_table_shell_wraps_caller_with_scroll_region():
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters["ago"] = _ago
    env.filters["ago_epoch"] = _ago_epoch
    env.filters["kilo"] = _kilo
    env.filters["spark_points"] = spark_points
    tpl = env.from_string(
        '{% import "_components.html" as c %}'
        '{% call c.history_table_shell("Sessions table") %}'
        "<table><caption>Sessions</caption></table>"
        "{% endcall %}"
    )
    html = tpl.render()
    assert 'class="table-scroll"' in html
    assert 'tabindex="0"' in html
    assert 'role="region"' in html
    assert 'aria-label="Sessions table"' in html
    assert "<table>" in html
    assert "<caption>Sessions</caption>" in html


def test_schedule_row_noninteractive_by_default():
    row = ScheduleRow(
        id="s1",
        project_id=7,
        project_name="Demo",
        prompt_preview="go",
        scheduled_for=1735700000,
        status="pending",
        error=None,
        scheduled_for_utc="2025-01-01 00:00 UTC",
        scheduled_for_iso="2025-01-01T00:00:00+00:00",
    )
    html = _module().schedule_row(row)
    assert 'data-scheduled-for="1735700000"' in html
    # The pre-JS/no-JS fallback is the readable UTC string with a machine
    # `datetime`, not the bare epoch int.
    assert 'datetime="2025-01-01T00:00:00+00:00"' in html
    assert "2025-01-01 00:00 UTC" in html
    assert html.count("1735700000") == 1  # only inside data-scheduled-for
    assert "Demo" in html
    assert "pending" in html
    assert "data-scheduled-run-now" not in html
    assert "data-scheduled-cancel" not in html


def test_schedule_row_omits_datetime_attr_when_iso_missing():
    # `scheduled_for_iso=None` is the out-of-range-epoch fallback
    # `_schedule_time_fields` returns; the `datetime` attribute must not be
    # emitted at all rather than rendering `datetime="None"`.
    row = ScheduleRow(
        id="s0",
        project_id=7,
        project_name="Demo",
        prompt_preview="go",
        scheduled_for=1735700000,
        status="pending",
        error=None,
        scheduled_for_utc="1735700000",
        scheduled_for_iso=None,
    )
    html = _module().schedule_row(row)
    assert "datetime=" not in html


def test_schedule_row_renders_error_when_present():
    row = ScheduleRow(
        id="s2",
        project_id=7,
        project_name="Demo",
        prompt_preview="go",
        scheduled_for=1735700000,
        status="failed",
        error="boom",
        scheduled_for_utc="2025-01-01 00:00 UTC",
        scheduled_for_iso="2025-01-01T00:00:00+00:00",
    )
    html = _module().schedule_row(row)
    assert "boom" in html
    assert "failed" in html


def test_schedule_row_interactive_pending_shows_run_now_edit_cancel():
    row = ScheduleRow(
        id="s3",
        project_id=7,
        project_name="Demo",
        prompt_preview="go",
        scheduled_for=1735700000,
        status="pending",
        error=None,
        scheduled_for_utc="2025-01-01 00:00 UTC",
        scheduled_for_iso="2025-01-01T00:00:00+00:00",
        mode="terminal",
    )
    html = _module().schedule_row(row, interactive=True)
    assert 'data-scheduled-job="s3"' in html
    assert 'data-scheduled-run-now="s3"' in html
    assert 'data-scheduled-edit-toggle="s3"' in html
    assert 'aria-expanded="false"' in html
    assert 'aria-controls="scheduled-edit-s3"' in html
    assert 'data-scheduled-cancel="s3"' in html
    assert 'data-scheduled-edit-panel="s3"' in html
    assert 'data-scheduled-edit-when="s3"' in html
    assert 'data-scheduled-edit-save="s3"' in html
    assert 'data-scheduled-state' in html
    assert 'data-scheduled-status="s3"' in html
    assert "terminal" in html
    # A `pending` row is never retryable, regardless of what a caller sets.
    assert "data-scheduled-retry=" not in html


def test_schedule_row_interactive_launching_omits_run_now_but_keeps_edit_cancel():
    row = ScheduleRow(
        id="s4",
        project_id=7,
        project_name="Demo",
        prompt_preview="go",
        scheduled_for=1735700000,
        status="launching",
        error=None,
        scheduled_for_utc="2025-01-01 00:00 UTC",
        scheduled_for_iso="2025-01-01T00:00:00+00:00",
    )
    html = _module().schedule_row(row, interactive=True)
    assert "data-scheduled-run-now" not in html
    assert 'data-scheduled-edit-toggle="s4"' in html
    assert 'data-scheduled-cancel="s4"' in html


def test_schedule_row_interactive_terminal_retryable_shows_retry_with_label():
    row = ScheduleRow(
        id="s5",
        project_id=7,
        project_name="Demo",
        prompt_preview="go",
        scheduled_for=1735700000,
        status="failed",
        error="boom",
        scheduled_for_utc="2025-01-01 00:00 UTC",
        scheduled_for_iso="2025-01-01T00:00:00+00:00",
        retryable=True,
    )
    html = _module().schedule_row(row, interactive=True)
    assert 'data-scheduled-retry="s5"' in html
    assert 'aria-label="Retry Demo run scheduled for 2025-01-01 00:00 UTC"' in html
    assert 'data-scheduled-retry-label="Retry Demo run scheduled for 2025-01-01 00:00 UTC"' in html
    assert 'data-scheduled-error' in html
    assert "data-scheduled-run-now" not in html
    assert "data-scheduled-cancel" not in html


def test_schedule_row_interactive_terminal_non_retryable_omits_retry():
    row = ScheduleRow(
        id="s6",
        project_id=7,
        project_name="Demo",
        prompt_preview="go",
        scheduled_for=1735700000,
        status="cancelled",
        error=None,
        scheduled_for_utc="2025-01-01 00:00 UTC",
        scheduled_for_iso="2025-01-01T00:00:00+00:00",
        retryable=False,
    )
    html = _module().schedule_row(row, interactive=True)
    assert "data-scheduled-retry=" not in html
