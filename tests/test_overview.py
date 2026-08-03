import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from bridge.agents import AgentsState
from bridge.api import create_app
from bridge.config import load
from bridge.models import GitState, Handoff, LiveSession, ScheduledRun, SessionRecord
from bridge.overview import (
    RECENT_LIMIT,
    UP_NEXT_LIMIT,
    build_overview,
)
from bridge.store import Store


def _cfg(tmp_path):
    return load({"db_path": tmp_path / "overview.db", "spool_dir": tmp_path / "spool"})


def _ended(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_attention_ladder_orders_kinds_and_pins_correct_hrefs(tmp_path):
    """Ladder ordering (handoff -> running -> stale -> schedule_failure) and
    the exact context-sensitive primary action for each kind, including the
    exact interpolated project id -- a mutation that swapped hrefs or ids
    must fail this."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000

    handoff_id = store.upsert_project("/p/handoff", "handoff-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoff", next_prompt="keep going",
        summary="finish the thing", created_at=now,
    ), handoff_id)

    running_id = store.upsert_project("/p/running", "running-project")

    stale_id = store.upsert_project("/p/stale", "stale-project")

    store.upsert_project("/p/failure-target", "failure-target")
    store.restore_scheduled_run(ScheduledRun(
        id="run-failed", project_path="/p/failure-target", prompt="do the thing",
        mode="interactive", scheduled_for=now - 100, created_at=now - 200,
        status="failed", completed_at=now - 50, error="boom",
    ))

    def probe(path: str) -> GitState:
        if path == "/p/stale":
            return GitState(
                status="ok", branch="main", dirty_count=3,
                oldest_uncommitted_at=now - 100 * 3600,
            )
        return GitState(status="ok", branch="main", dirty_count=0)

    def agents_fn() -> AgentsState:
        return AgentsState(status="ok", sessions=[LiveSession(
            session_id="live-1", cwd="/p/running", kind="interactive", status="busy",
        )])

    model = build_overview(store, cfg, now=now, probe_fn=probe, agents_fn=agents_fn)

    kinds = [item.kind for item in model.attention]
    assert kinds == ["handoff", "running", "stale"]
    assert model.attention_total == 4

    handoff_item, running_item, stale_item = model.attention

    assert handoff_item.project_id == handoff_id
    assert handoff_item.primary_action.label == "Continue in Terminal"
    assert handoff_item.primary_action.href == f"/project/{handoff_id}?tab=current"
    assert model.attention[0].meta["path"] == "/p/handoff"  # hero renders a path footer

    assert running_item.project_id == running_id
    assert running_item.primary_action.label == "Open project"
    assert running_item.primary_action.href == f"/project/{running_id}"

    assert stale_item.project_id == stale_id
    assert stale_item.primary_action.label == "Review project state"
    assert stale_item.primary_action.href == f"/project/{stale_id}"

    store.close()


def test_attention_is_bounded_without_repeating_omitted_risks_as_recent(tmp_path):
    """Removing the cap recreates the multi-screen card wall; deriving Recent
    from only the displayed slice repeats omitted stale projects as if quiet."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 100_000
    expected_limit = 3
    for i in range(expected_limit + 2):
        store.upsert_project(f"/p/stale-{i}", f"stale-{i}")
    quiet_id = store.upsert_project("/p/quiet", "quiet")

    def probe(path: str) -> GitState:
        if path.startswith("/p/stale-"):
            return GitState(
                status="ok", branch="main", dirty_count=1,
                oldest_uncommitted_at=now - 100 * 3600,
            )
        return GitState(status="ok", branch="main", dirty_count=0)

    model = build_overview(
        store, cfg, now=now, probe_fn=probe,
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert len(model.attention) == expected_limit
    assert model.attention_total == expected_limit + 2
    assert [item.title for item in model.attention] == [
        f"stale-{i}" for i in range(expected_limit)
    ]
    assert [row.project_id for row in model.recent] == [quiet_id]
    store.close()


def test_schedule_failure_excludes_already_retried_originals(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    store.upsert_project("/p/retried", "retried-project")
    store.upsert_project("/p/unretried", "unretried-project")

    # The original failure: its status stays "failed" forever once retried.
    store.restore_scheduled_run(ScheduledRun(
        id="orig", project_path="/p/retried", prompt="p", mode="interactive",
        scheduled_for=now - 500, created_at=now - 600, status="failed",
        completed_at=now - 400, error="boom",
    ))
    # The retry: a fresh row that references the original via `retry_of`.
    store.restore_scheduled_run(ScheduledRun(
        id="retry-1", project_path="/p/retried", prompt="p", mode="interactive",
        scheduled_for=now + 100, created_at=now - 300, status="pending",
        retry_of="orig",
    ))
    # A genuinely unretried failure must still surface.
    store.restore_scheduled_run(ScheduledRun(
        id="unretried", project_path="/p/unretried", prompt="p",
        mode="interactive", scheduled_for=now - 500, created_at=now - 600,
        status="failed", completed_at=now - 350, error="oops",
    ))

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    failure_run_ids = [item.meta["run_id"] for item in model.attention
                       if item.kind == "schedule_failure"]
    assert failure_run_ids == ["unretried"]

    store.close()


def test_schedule_failures_keep_true_total_and_show_newest_three_first(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    completed_ats = [100, 500, 300, 700, 200, 600, 400]
    for i, completed_at in enumerate(completed_ats):
        path = f"/p/fail{i}"
        store.upsert_project(path, f"fail-project-{i}")
        store.restore_scheduled_run(ScheduledRun(
            id=f"fail-{i}", project_path=path, prompt="p", mode="interactive",
            scheduled_for=now - 1000, created_at=now - 1000, status="failed",
            completed_at=completed_at, error="boom",
        ))

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    failures = [item for item in model.attention if item.kind == "schedule_failure"]
    assert len(failures) == 3
    assert model.attention_total == len(completed_ats)
    completed_order = [item.meta["run_id"] for item in failures]
    expected_order = [
        f"fail-{i}" for i, _ in sorted(
            enumerate(completed_ats), key=lambda p: p[1], reverse=True,
        )
    ][:3]
    assert completed_order == expected_order
    assert all(item.primary_action.label == "Review scheduled run" for item in failures)
    assert all(item.primary_action.href == "/schedule" for item in failures)

    store.close()


def test_recent_excludes_projects_already_in_attention(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    handoff_id = store.upsert_project("/p/handoff", "handoff-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoff", next_prompt="keep going", created_at=now,
    ), handoff_id)
    store.upsert_project("/p/quiet1", "quiet-1")
    store.upsert_project("/p/quiet2", "quiet-2")

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert handoff_id in [item.project_id for item in model.attention]
    assert handoff_id not in [row.project_id for row in model.recent]

    store.close()


def test_recent_truncates_to_limit_and_pin_sorts_first(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000

    pinned_id = store.upsert_project("/p/pinned", "pinned")
    store.set_project_pinned(pinned_id, True)
    store.upsert_session(SessionRecord(
        session_id="s-pinned", transcript_path="/t/pinned", ended_at=_ended(5),
    ), pinned_id)

    for i in range(6):
        store.upsert_project(f"/p/quiet{i}", f"quiet-{i}")

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    # 7 quiet/pinned projects seeded, none of them attention-worthy; `recent`
    # still truncates to RECENT_LIMIT.
    assert len(model.recent) == RECENT_LIMIT
    assert model.recent[0].project_id == pinned_id
    assert model.recent[0].pinned is True
    assert model.recent[0].status_word == "recent"

    store.close()


def test_up_next_is_chronological_and_truncated(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    store.upsert_project("/p/sched", "sched-project")
    for i, offset in enumerate([400, 100, 300, 200]):
        store.create_scheduled_run(ScheduledRun(
            id=f"pending-{i}", project_path="/p/sched", prompt="p",
            mode="interactive", scheduled_for=now + offset, created_at=now,
        ))

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert len(model.up_next) == UP_NEXT_LIMIT
    assert [row.scheduled_for for row in model.up_next] == sorted(
        row.scheduled_for for row in model.up_next
    )
    assert model.up_next[0].scheduled_for == now + 100
    # `<time>`'s pre-JS/no-JS fallback: a readable UTC string plus an ISO
    # `datetime` attribute, not the bare epoch int a screen reader or a
    # JS-disabled load would otherwise see.
    assert model.up_next[0].scheduled_for_utc
    assert "UTC" in model.up_next[0].scheduled_for_utc
    assert model.up_next[0].scheduled_for_iso is not None

    store.close()


def test_last_session_age_floors_at_zero_under_clock_skew(tmp_path):
    """A poll/write race or clock skew can put a session's `ended_at` at or
    after the `now` Overview is built with; `last_session_age_seconds` must
    floor at 0 rather than go negative (which would render as "-2m ago")."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000

    project_id = store.upsert_project("/p/skewed", "skewed-project")
    store.upsert_session(SessionRecord(
        session_id="s-skewed", transcript_path="/t/skewed", ended_at=_ended(5),
    ), project_id)

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    # `_ended(5)` is five minutes before *real* wall-clock time, whose epoch
    # is far larger than this test's artificial `now=10_000` -- exactly the
    # "ended >= now" skew this guards against.
    assert len(model.recent) == 1
    assert model.recent[0].last_session_age_seconds == 0

    store.close()


def test_totals_freshness_and_diagnostics_reuse_dashboard_envelope(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    store.upsert_project("/p/only", "only-project")
    store.record_index_run({"parse_errors": 0}, ran_at=now - 10, duration_ms=1)

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model.freshness["server"] == "available"
    assert model.freshness["index_at"] == now - 10
    assert model.totals["projects"] == 1
    assert model.diagnostics_alert is False

    store.close()


def test_no_handoff_or_schedule_failure_yields_empty_attention(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000
    store.upsert_project("/p/quiet", "quiet-project")

    model = build_overview(
        store, cfg, now=now,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model.attention == []

    store.close()


# --- Route: GET / renders the calm Overview (Task 2.3) -----------------------


def _route_client(tmp_path, name="route"):
    cfg = load({"db_path": tmp_path / f"{name}.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    return TestClient(create_app(store, cfg)), store, cfg


def test_overview_route_has_no_textarea_or_launch_selectors(tmp_path):
    """The compose/launch/handoff surface belongs to the Project workspace now
    (spec:Milestone-2's calm-Overview requirement); `/` must carry none of it."""
    c, store, _ = _route_client(tmp_path)
    store.upsert_project("/p/quiet", "quiet-project")

    html = c.get("/").text

    assert html.count("<textarea") == 0
    assert "data-launch-model" not in html
    assert "data-compose-prompt" not in html
    store.close()


def test_overview_route_shows_attention_recent_link_and_freshness(tmp_path):
    c, store, _ = _route_client(tmp_path)
    pid = store.upsert_project("/p/handoff", "handoff-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoff", next_prompt="keep going", created_at=1,
    ), pid)

    html = c.get("/").text

    assert ">Needs attention<" in html
    assert "Continue in Terminal" in html
    assert f'href="/project/{pid}?tab=current"' in html
    assert '<a href="/projects">View all projects</a>' in html
    assert "data-freshness-strip" in html
    assert re.search(r'data-freshness-state="\w+"', html)
    assert 'data-dashboard-refresh>Refresh</button>' in html
    store.close()


def test_overview_route_renders_command_strip_with_hot_and_cold_branches(tmp_path):
    """The six-cell command strip is server-rendered on every load. One queued
    handoff makes attention_total == 1 (with running == 0 and no real git repo
    behind the seeded path, so dirty == 0 too) -- exactly the state that
    exercises both sides of the is-hot/is-live conditional: attention lights
    up, running does not."""
    c, store, _ = _route_client(tmp_path)
    pid = store.upsert_project("/p/handoff", "handoff-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoff", next_prompt="keep going", created_at=1,
    ), pid)

    html = c.get("/").text

    assert 'class="overview-command-strip"' in html
    for label in ("Running", "Needs attention", "Queued", "Dirty trees", "Scheduled", "Projects"):
        assert label in html

    assert re.search(
        r'<div class="overview-command-strip" role="group" aria-label="[^"]*">',
        html,
    )

    assert "is-hot" in html
    assert "is-live" not in html

    assert re.search(
        r'class="command-cell is-hot">\s*<span class="command-cell__num">1</span>',
        html,
    )
    store.close()


def test_overview_route_uses_compact_primary_and_secondary_composition(tmp_path):
    """The approved stage is one focal object plus at most two compact cards.

    Rendering every attention item as an equal row recreates the pre-approved
    wall even when the model is bounded, so source structure is part of the
    regression contract here.
    """
    c, store, _ = _route_client(tmp_path)
    for index in range(5):
        path = f"/p/handoff-{index}"
        pid = store.upsert_project(path, f"handoff-project-{index}")
        store.upsert_session(SessionRecord(
            session_id=f"s{index}", transcript_path=f"/t/s{index}",
            title=f"Narrative next step {index}", ended_at=_ended(index + 1),
        ), pid)
        store.create_handoff(Handoff(
            id=f"h{index}", project_path=path, next_prompt="keep going",
            source_session_id=f"s{index}", summary=f"Finish queued work {index}",
            created_at=index + 1,
        ), pid)

    html = c.get("/").text

    assert html.count('class="attention-primary ') == 1
    assert html.count('class="attention-secondary ') == 2
    assert 'class="overview-attention-stage"' in html
    assert 'class="overview-lower-grid"' in html
    attention = html[html.index('<section class="overview-attention"'):]
    attention = attention[:attention.index('<section class="overview-lower-grid"')]
    assert '<a href="/projects">View all projects</a>' in attention
    assert '<a href="/schedule">Open schedule</a>' in attention
    assert attention.index("Narrative next step 0") < attention.index("Continue in Terminal")
    assert html.index('class="overview-attention-stage"') < html.index(
        'class="overview-lower-grid"'
    )
    store.close()


def _hero(html: str) -> str:
    """The `.attention-primary` article only -- scoping every hero assertion to
    it keeps a secondary mini-card's markup from satisfying one by accident."""
    hero = html[html.index('<article class="attention-primary '):]
    return hero[:hero.index("</article>")]


def _hero_actions(html: str) -> str:
    hero = _hero(html)
    actions = hero[hero.index('<div class="attention-primary__actions">'):]
    return actions[:actions.index("</div>")]


def test_handoff_hero_ghost_action_is_not_a_duplicate_of_the_primary(tmp_path):
    """One primary action per view, and the ghost beside it must go somewhere
    else. A handoff's own `primary_action` is already `?tab=current`
    (`overview.py`'s handoff branch), so pointing the ghost at
    `primary_action.href` -- or at the generic `?tab=current` fallback -- makes
    it a second button to the identical URL: two controls, one destination,
    which is exactly the redundant twin the spec forbids.
    """
    c, store, _ = _route_client(tmp_path)
    pid = store.upsert_project("/p/handoff", "handoff-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoff", next_prompt="keep going", created_at=1,
    ), pid)

    actions = _hero_actions(c.get("/").text)
    hrefs = re.findall(r'<a class="btn[^"]*" href="([^"]+)"', actions)

    assert len(hrefs) == 2, f"hero should offer a primary and one ghost: {hrefs}"
    assert len(set(hrefs)) == 2, f"ghost duplicates the primary's href: {hrefs}"
    assert f"/project/{pid}?tab=current" in hrefs
    assert f"/project/{pid}" in hrefs
    assert actions.count("btn--primary") == 1
    store.close()


def test_running_hero_keeps_its_working_tree_without_a_last_activity_row(tmp_path,
                                                                        monkeypatch):
    """A running item's `meta` carries `branch`/`dirty_count` but never a
    `created_at` (`overview.py`'s running branch), so guarding the branch and
    dirty count behind a "Last activity" timestamp silently drops both from the
    hero -- the facts the retired `.attention-primary__meta` strip always
    showed. They belong to their own labelled row: calling a branch name "Last
    activity" would be a lie, and a live session has no ended-at to report.
    """
    from bridge import agents
    import bridge.cards as cards_mod

    monkeypatch.setattr(agents, "probe", lambda *a, **k: AgentsState(
        status="ok", sessions=[LiveSession(
            session_id="live-1", cwd="/p/running", kind="interactive",
            status="busy",
        )],
    ))
    monkeypatch.setattr(cards_mod.gitprobe, "probe", lambda p: GitState(
        status="ok", branch="feature/almanac", dirty_count=4,
    ))

    c, store, _ = _route_client(tmp_path)
    store.upsert_project("/p/running", "running-project")

    hero = _hero(c.get("/").text)

    assert "<dt>Working tree</dt>" in hero
    assert "feature/almanac · 4 dirty" in hero
    assert "<dt>Last activity</dt>" not in hero
    assert "<dt>Waiting on</dt>" not in hero
    store.close()


def test_working_tree_row_separates_branch_and_dirty_only_when_it_has_both(tmp_path,
                                                                          monkeypatch):
    """The row is built from two independently-absent facts, so the separator
    has to belong to the pair rather than to either half. A stray leading or
    trailing ` · ` -- or a literal `None` from an unguarded null branch, which
    `GitState.branch` permits -- is the failure mode."""
    from bridge import agents
    import bridge.cards as cards_mod

    monkeypatch.setattr(agents, "probe", lambda *a, **k: AgentsState(
        status="ok", sessions=[LiveSession(
            session_id="live-1", cwd="/p/running", kind="interactive",
            status="busy",
        )],
    ))

    cases = [
        ("both", "feature/almanac", 4, "feature/almanac · 4 dirty"),
        ("branch-only", "feature/almanac", 0, "feature/almanac"),
        ("dirty-only", None, 4, "4 dirty"),
    ]
    for name, branch, dirty, expected in cases:
        monkeypatch.setattr(cards_mod.gitprobe, "probe", lambda p, branch=branch,
                            dirty=dirty: GitState(
            status="ok", branch=branch, dirty_count=dirty,
        ))
        c, store, _ = _route_client(tmp_path, name=f"tree-{name}")
        store.upsert_project("/p/running", "running-project")

        hero = _hero(c.get("/").text)
        row = re.search(r"<dt>Working tree</dt>\s*<dd>(.*?)</dd>", hero, re.S)

        assert row is not None, f"{name}: no working-tree row"
        assert row.group(1).strip() == expected, f"{name}: {row.group(1)!r}"
        assert "None" not in row.group(1), f"{name}: null branch leaked"
        store.close()


def test_overview_route_keeps_freshness_strip_and_total_hooks_for_live_js(tmp_path):
    c, store, _ = _route_client(tmp_path)
    store.upsert_project("/p/quiet", "quiet-project")

    html = c.get("/").text

    assert "data-freshness-strip" in html
    assert html.count("data-dashboard-total=") == 8
    assert len(re.findall(r"<dd[^>]+data-dashboard-total=", html)) == 8
    assert "data-project-membership-status" in html
    assert "data-diagnostics-alert" in html
    store.close()
