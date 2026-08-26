import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from bridge.api import create_app
from bridge.cards import build_cards
from bridge.config import load
from bridge.models import (
    AgentsState,
    GitState,
    Handoff,
    Launch,
    LiveSession,
    SessionRecord,
)
from bridge.store import Store
from bridge.workspace import build_workspace


def _cfg(tmp_path):
    return load({"db_path": tmp_path / "workspace.db", "spool_dir": tmp_path / "spool"})


def _ended(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_unknown_project_id_returns_none(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)

    model = build_workspace(
        store, cfg, 999999, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is None
    store.close()


def test_queued_handoff_surfaces_and_matches_the_cards_handoff(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/handoff", "handoff-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoff", next_prompt="keep going",
        summary="finish the thing", created_at=1,
    ), pid)

    model = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert model.handoff is not None
    assert model.handoff["id"] == "h1"
    assert model.handoff == model.card.handoff
    store.close()


def test_card_identity_matches_a_direct_build_cards_call(tmp_path):
    """The workspace's `card` must be the same projection Overview/Projects
    render -- not a second, divergently-built Card -- so its fields must
    match a direct `build_cards` call filtered to this project id."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/one", "one-project")
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", title="a session",
        ended_at=_ended(5),
    ), pid)

    def agents_fn():
        return AgentsState(status="ok", sessions=[])

    model = build_workspace(store, cfg, pid, "current", agents_fn=agents_fn)
    assert model is not None
    assert model.card.project_id == pid

    reference_cards = build_cards(
        store, cfg,
        probe_fn=lambda _p: GitState(status="unavailable"),
        agents_fn=agents_fn, debouncer=None, hook_state=None,
    )
    reference = next(c for c in reference_cards if c.project_id == pid)

    assert model.card.name == reference.name
    assert model.card.path == reference.path
    assert model.card.session == reference.session
    assert model.card.launch_models == reference.launch_models
    assert model.card.launch_efforts == reference.launch_efforts
    assert model.card.launch_permission_modes == reference.launch_permission_modes
    store.close()


def test_unknown_tab_normalizes_to_current(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/tab", "tab-project")

    model = build_workspace(
        store, cfg, pid, "zzz",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert model.tab == "current"
    store.close()


def test_missing_tab_normalizes_to_current(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/tab2", "tab-project-2")

    model = build_workspace(
        store, cfg, pid, None,
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert model.tab == "current"
    store.close()


def test_workspace_never_live_probes_git(tmp_path, monkeypatch):
    """No `probe_fn` is injected here. `build_workspace` must still never let
    `build_cards` fall through to a LIVE git probe -- it always forces the
    cache-reading `probe_fn` stand-in itself. Proven with a spy on the actual
    name `build_cards` resolves at call time (`bridge.cards.gitprobe.probe`,
    since `build_cards` does `if probe_fn is None: probe_fn = gitprobe.probe`)
    rather than by an inference from whether the call happens to raise: a
    plain non-existent path does NOT make `gitprobe.probe` raise (it
    short-circuits to `GitState(status="unavailable")`), so a test that only
    checked for an exception would pass whether or not the override was ever
    actually wired in."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/does-not-exist-on-disk", "cached-git")
    store.put_git_cache(
        pid,
        GitState(status="ok", branch="main", dirty_count=3,
                  oldest_uncommitted_at=100),
        probed_at=500,
    )

    calls = []
    import bridge.cards
    monkeypatch.setattr(
        bridge.cards.gitprobe, "probe",
        lambda path: calls.append(path) or GitState(status="unavailable"),
    )

    model = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert calls == []  # no live probe fired
    assert model is not None
    assert model.git is not None
    assert model.git.cached_at == 500
    assert model.git.branch == "main"
    assert model.git.dirty_count == 3
    store.close()


def test_no_git_cache_yields_none_git(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/no-cache", "no-cache-project")

    model = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert model.git is None
    store.close()


def test_sessions_tab_populates_only_sessions_list(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/sessions", "sessions-project")
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", title="first",
        ended_at=_ended(10),
    ), pid)
    store.upsert_session(SessionRecord(
        session_id="s2", transcript_path="/t/s2", title="second",
        ended_at=_ended(5),
    ), pid)
    store.create_handoff(Handoff(
        id="h1", project_path="/p/sessions", next_prompt="next", created_at=1,
    ), pid)

    model = build_workspace(
        store, cfg, pid, "sessions",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert len(model.sessions) == 2
    assert model.handoffs == []
    assert model.launches == []
    store.close()


def test_handoffs_tab_populates_only_handoffs_list(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/handoffs-tab", "handoffs-tab-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/handoffs-tab", next_prompt="next", created_at=1,
    ), pid)
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", ended_at=_ended(5),
    ), pid)

    model = build_workspace(
        store, cfg, pid, "handoffs",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert len(model.handoffs) == 1
    assert model.sessions == []
    assert model.launches == []
    store.close()


def test_current_tab_leaves_history_lists_empty(tmp_path):
    """The Current tab draws its handoff/session view off `card`, not the
    50-capped history reads -- those stay unfetched (empty) when unselected."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/current", "current-project")
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", ended_at=_ended(5),
    ), pid)

    model = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert model.sessions == []
    assert model.handoffs == []
    assert model.launches == []
    store.close()


def test_history_tab_reports_its_total_and_pages_by_offset(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/paged", "paged-project")
    for i in range(5):
        store.upsert_session(SessionRecord(
            session_id=f"s{i}", transcript_path=f"/t/s{i}", title=f"s{i}",
            ended_at=_ended(50 - i),
        ), pid)

    first = build_workspace(
        store, cfg, pid, "sessions", page=0, page_size=2,
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )
    assert first.history_total == 5
    assert first.page == 0
    assert first.page_size == 2
    assert len(first.sessions) == 2

    second = build_workspace(
        store, cfg, pid, "sessions", page=1, page_size=2,
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )
    assert [s["id"] for s in second.sessions] == [
        s["id"] for s in store.sessions(pid, limit=2, offset=2)
    ]
    assert second.history_total == 5

    # A page past the end is empty but still reports the true total, so the
    # pager can render "0 of 5" with a working Previous rather than a bare table.
    beyond = build_workspace(
        store, cfg, pid, "sessions", page=9, page_size=2,
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )
    assert beyond.sessions == []
    assert beyond.history_total == 5
    store.close()


def test_current_tab_reports_zero_history_total(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/cur-total", "cur")
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", ended_at=_ended(5),
    ), pid)
    model = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )
    assert model.history_total == 0
    store.close()


def test_session_metas_reads_only_for_populated_sessions_list(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/meta", "meta-project")
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", ended_at=_ended(5),
    ), pid)
    (cfg.session_meta_dir).mkdir(parents=True, exist_ok=True)
    (cfg.session_meta_dir / "s1.json").write_text(
        '{"session_id": "s1", "files_modified": 3}'
    )

    model = build_workspace(
        store, cfg, pid, "sessions",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is not None
    assert "s1" in model.session_metas
    assert model.session_metas["s1"].files_modified == 3
    store.close()


def test_live_state_is_threaded_through_without_a_second_probe(tmp_path):
    """Passing `live_state` directly (mirroring `DashboardBuilder.full_update`)
    must be reflected on the card without requiring `agents_fn`."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/live", "live-project")

    live_state = AgentsState(status="ok", sessions=[LiveSession(
        session_id="live-1", cwd="/p/live", kind="interactive", status="busy",
    )])

    model = build_workspace(store, cfg, pid, "current", live_state=live_state)

    assert model is not None
    assert model.card.live is not None
    assert model.card.live.session_id == "live-1"
    store.close()


def test_hidden_project_yields_none(tmp_path):
    """A project row can exist while `build_cards` (which reads active
    projects only) never produces a card for it -- no card means no
    workspace."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/hidden", "hidden-project")
    store.set_project_status(pid, "hidden")

    model = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model is None
    store.close()


# --- Task 3.2: the tabbed `/project/{id}` route -----------------------------


def _client(tmp_path):
    cfg = load({"db_path": tmp_path / "route.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/workspace", "workspace-project")
    return TestClient(create_app(store, cfg)), store, pid


def _active_tab(html, pid):
    """The one `?tab=` value whose link carries `aria-current="page"`.

    `aria-current="page"` legitimately appears more than once on the page --
    the breadcrumb's own current-page span and the sidebar's "Projects"
    section both carry it too -- so this only looks inside the tab links
    themselves, not at the page-wide count.
    """
    matches = re.findall(
        rf'href="/project/{pid}\?tab=(\w+)"\s*\n?\s*aria-current="page"', html,
    )
    assert len(matches) == 1, (matches, html.count('aria-current="page"'))
    return matches[0]


def _active_tab_count(html, pid):
    return len(re.findall(
        rf'href="/project/{pid}\?tab=\w+"\s*\n?\s*aria-current="page"', html,
    ))


def test_get_project_defaults_to_the_current_tab(tmp_path):
    c, store, pid = _client(tmp_path)
    r = c.get(f"/project/{pid}")
    assert r.status_code == 200
    assert _active_tab(r.text, pid) == "current"
    store.close()


def test_each_tab_query_value_selects_that_tab(tmp_path):
    c, store, pid = _client(tmp_path)
    for tab in ("sessions", "handoffs", "launches"):
        html = c.get(f"/project/{pid}?tab={tab}").text
        assert _active_tab(html, pid) == tab
        # Only the matching tab link carries it, not the other three.
        assert _active_tab_count(html, pid) == 1
    store.close()


def test_an_unknown_tab_falls_back_to_current_rather_than_blank(tmp_path):
    c, store, pid = _client(tmp_path)
    r = c.get(f"/project/{pid}?tab=zzz")
    assert r.status_code == 200
    assert _active_tab(r.text, pid) == "current"
    assert "<table" not in r.text
    store.close()


def test_an_invalid_project_id_is_still_a_404(tmp_path):
    c, store, _ = _client(tmp_path)
    assert c.get("/project/999999").status_code == 404
    store.close()


def test_breadcrumb_links_to_the_projects_index(tmp_path):
    c, store, pid = _client(tmp_path)
    html = c.get(f"/project/{pid}").text
    assert '<a href="/projects">Projects</a>' in html
    store.close()


def test_the_workspace_page_has_exactly_one_h1(tmp_path):
    c, store, pid = _client(tmp_path)
    html = c.get(f"/project/{pid}").text
    assert len(re.findall(r"<h1\b", html)) == 1
    store.close()


def test_pin_and_hide_hooks_are_present_and_keyed_off_the_project_id(tmp_path):
    c, store, pid = _client(tmp_path)
    html = c.get(f"/project/{pid}").text
    assert f'data-project-pin="{pid}"' in html
    assert f'data-project-hide="{pid}"' in html
    assert 'aria-pressed="false"' in html
    store.close()


def test_the_current_tab_never_carries_history_table_markup(tmp_path):
    c, store, pid = _client(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    assert "<table" not in html
    store.close()


def test_history_tabs_never_carry_the_current_tabs_context_rail(tmp_path):
    c, store, pid = _client(tmp_path)
    for tab in ("sessions", "handoffs", "launches"):
        html = c.get(f"/project/{pid}?tab={tab}").text
        assert 'class="workspace-rail"' not in html
        assert 'workspace-side-card--state' not in html
    store.close()


# --- Task 3.3: the Current tab's continuation surface -----------------------


def _client_with_handoff(tmp_path, with_session=True):
    cfg = load({"db_path": tmp_path / "current.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/current-ui", "current-ui-project")
    if with_session:
        store.upsert_session(SessionRecord(
            session_id="s1", transcript_path="/t/s1", title="Did the work",
            ended_at=_ended(5),
        ), pid)
    store.create_handoff(Handoff(
        id="h1", project_path="/p/current-ui", next_prompt="keep going " * 30,
        summary="finish the thing", created_at=1,
    ), pid)
    return TestClient(create_app(store, cfg)), store, pid


def test_current_tab_exposes_interactive_hooks_keyed_off_the_queued_handoff(tmp_path):
    """Task 5 fix round 1: the launch band renders once per queued handoff, so
    its `lid` (and every id/data-* derived from it) is keyed off the HANDOFF's
    own id, not the project id -- a project-id-keyed `lid` would repeat
    verbatim across stacked bands and collide."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    lid = "launch-h1"
    assert f'data-launch="{lid}"' in html
    assert f'data-launch-model="{lid}"' in html
    assert f'data-launch-perm="{lid}"' in html
    assert f'data-launch-button="{lid}"' in html
    assert 'data-prompt-handoff="h1"' in html
    store.close()


def test_permission_select_defaults_to_the_no_flag_option_not_a_suggestion(tmp_path):
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    lid = "launch-h1"
    perm_block = html.split(f'data-launch-perm="{lid}"', 1)[1].split("</select>", 1)[0]
    first_option = perm_block.split("<option", 2)[1]
    assert "selected" in first_option
    assert "danger" not in first_option
    store.close()


def test_exactly_one_primary_button_when_a_handoff_is_queued(tmp_path):
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    assert html.count("btn--primary") == 1
    assert "Continue in Terminal" in html
    store.close()


def test_exactly_one_primary_button_with_no_handoff(tmp_path):
    c, store, pid = _client(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    assert html.count("btn--primary") == 1
    assert "Start session" in html
    store.close()


def test_empty_state_primary_button_drives_the_compose_prompt_and_starts_disabled(
    tmp_path,
):
    """With no queued handoff the primary action IS the compose box: the band
    points `data-launch-prompt` at the ad hoc textarea and the button renders
    `disabled` (compose starts empty), so "Start session" can never post an
    empty body that would 422."""
    c, store, pid = _client(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    lid = f"launch-{pid}"
    band = html.split(f'data-launch="{lid}"', 1)[1].split(">", 1)[0]
    assert f'data-launch-prompt="compose-{pid}"' in band
    assert "data-launch-handoff" not in band
    # The primary button in this band renders disabled until there is text.
    button = html.split(f'data-launch-button="{lid}"', 1)[1].split(">", 1)[0]
    assert "disabled" in button
    store.close()


def test_handoff_primary_button_is_never_disabled(tmp_path):
    """A queued handoff already has a prompt, so its "Continue in Terminal"
    button is launchable immediately -- never disabled like the empty state."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    lid = "launch-h1"
    button = html.split(f'data-launch-button="{lid}"', 1)[1].split(">", 1)[0]
    assert "disabled" not in button
    store.close()


def test_current_tab_headings_are_sequential_with_no_skipped_level(tmp_path):
    """The page `h1` is followed by the compose/handoff section titles at `h2`
    -- not `h3`, which would skip a level below the project heading (WCAG 2.2
    heading-order). The history tabs already use `h2`."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    assert '<h2 class="handoff__title"' in html
    assert '<h2 class="compose__title"' in html
    # No level is skipped: the section titles are not h3 under the project h1.
    assert '<h3 class="compose__title"' not in html
    assert '<h3 class="handoff__title"' not in html
    store.close()


def test_span_line_renders_only_when_session_and_handoff_both_exist(tmp_path):
    c, store, pid = _client_with_handoff(tmp_path / "with-session", with_session=True)
    assert "workspace-span" in c.get(f"/project/{pid}?tab=current").text
    store.close()

    c2, store2, pid2 = _client_with_handoff(
        tmp_path / "no-session", with_session=False
    )
    assert "workspace-span" not in c2.get(f"/project/{pid2}?tab=current").text
    store2.close()

    c3, store3, pid3 = _client(tmp_path / "no-handoff")
    assert "workspace-span" not in c3.get(f"/project/{pid3}?tab=current").text
    store3.close()


def test_current_tab_leads_with_the_real_continuation_span_and_primary_action(tmp_path):
    """A prose `title -> next: full summary` line or telemetry-first layout
    loses the approved Bridge signature and makes the next action secondary."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text

    continuation = html.index('class="continuation-panel"')
    primary = html.index("btn--primary")
    state = html.index('class="workspace-side-card workspace-side-card--state"')

    assert continuation < primary < state
    for label in ("Session ended", "Handoff ready", "Next session"):
        assert label in html
    span = html[html.index('class="workspace-span"'):]
    span = span[:span.index("</div>")]
    assert "finish the thing" not in span, "the span labels states, not a duplicated summary"
    store.close()


def test_current_tab_matches_the_approved_continuation_and_right_rail_structure(
    tmp_path,
):
    """Equal-weight telemetry and a plaintext prompt are the known visual
    regression; this protects the focal card, nested prompt, and structured
    supporting rail without asserting pixel values.
    """
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text

    assert '<h2 class="workspace-current__title">Next session</h2>' in html
    assert "Saved from the last handoff and ready to continue." in html
    assert 'class="pill pill--work continuation-status"' in html
    assert 'class="handoff-prompt"' in html
    assert "Saved prompt" in html
    assert 'class="workspace-side-card workspace-side-card--state"' in html
    assert '<h2>Project state</h2>' in html
    assert 'class="workspace-side-card workspace-side-card--activity"' in html
    assert '<h2>Recent activity</h2>' in html
    assert html.index('class="continuation-panel"') < html.index("btn--primary")
    assert html.index("btn--primary") < html.index(
        'class="workspace-side-card workspace-side-card--state"'
    )
    for label in ("Session ended", "Handoff ready", "Next session"):
        assert label in html
    store.close()


def test_project_header_is_contextual_and_current_tab_uses_approved_label(tmp_path):
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text

    page_head = html[html.index('<header class="page-head"'):html.index("</header>")]
    assert 'class="breadcrumb"' in page_head
    assert 'class="card__path project-path"' in page_head
    assert f'data-project-pin="{pid}"' in page_head
    assert f'data-project-hide="{pid}"' in page_head
    assert ">Current work</a>" in html
    store.close()


def test_queued_handoff_shows_its_age_beside_the_summary(tmp_path):
    """Spec line 180: the queued handoff shows its summary AND its age, so a
    stale next step reads as stale. `created_at` is on every handoff row."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    handoff = html.split('class="handoff"', 1)[1].split("</section>", 1)[0]
    assert "finish the thing" in handoff, "the summary still renders"
    assert 'class="handoff__kicker"' in handoff
    assert "Queued handoff" in handoff and "ago" in handoff
    store.close()


def test_two_handoffs_render_two_blocks(tmp_path):
    """`Card.handoffs` (Task 2) can carry more than one queued handoff -- the
    Current tab must stack a fireable block per handoff, not just the newest
    (`card.handoff`, the compat property)."""
    cfg = load({"db_path": tmp_path / "two.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/two-handoffs", "two-handoffs-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/two-handoffs", next_prompt="plan",
        summary="Planned", created_at=1,
    ), pid)
    store.create_handoff(Handoff(
        id="h2", project_path="/p/two-handoffs", next_prompt="ui",
        summary="UI work", created_at=2,
    ), pid)

    c = TestClient(create_app(store, cfg))
    html = c.get(f"/project/{pid}?tab=current").text

    assert 'data-handoff-section="h1"' in html
    assert 'data-handoff-section="h2"' in html
    assert 'data-launch-handoff="h1"' in html
    assert 'data-launch-handoff="h2"' in html
    store.close()


def test_stacked_launch_bands_get_unique_ids_per_handoff(tmp_path):
    """Task 5 fix round 1 (CRITICAL): before this fix `launch_band`'s `lid` was
    keyed off `card.project_id`, the SAME string for every stacked band --
    every `<select id>`, `data-launch`, `data-launch-model/-effort/-perm`, and
    `data-launch-button` repeated verbatim across both handoffs. `launch.js`
    resolves each of those by an exact-match `document.querySelector`, so a
    click on the SECOND handoff's launch button would have silently read the
    FIRST handoff's model/effort/permission selects. Keying `lid` off each
    handoff's own id (not the project id) is what makes every id unique per
    band, so the two selects below can never collide."""
    cfg = load({"db_path": tmp_path / "stacked.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/stacked", "stacked-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/stacked", next_prompt="plan",
        summary="Planned", created_at=1,
    ), pid)
    store.create_handoff(Handoff(
        id="h2", project_path="/p/stacked", next_prompt="ui",
        summary="UI work", created_at=2,
    ), pid)

    c = TestClient(create_app(store, cfg))
    html = c.get(f"/project/{pid}?tab=current").text

    # Every id on the page is unique -- the real symptom a collision produces.
    ids = re.findall(r'\sid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), f"duplicate ids: {sorted(ids)}"

    for hid in ("h1", "h2"):
        lid = f"launch-{hid}"
        assert f'data-launch="{lid}"' in html
        assert f'data-launch-model="{lid}"' in html
        assert f'data-launch-effort="{lid}"' in html
        assert f'data-launch-perm="{lid}"' in html
        assert f'data-launch-button="{lid}"' in html
        assert f'id="{lid}-model"' in html
        assert f'id="{lid}-effort"' in html
        assert f'id="{lid}-perm"' in html
        assert f'for="{lid}-model"' in html

    # Neither band is a stray project-id-keyed leftover. (The compose box's
    # own `data-compose-launch` is keyed off its own `cid`, not `lid` --
    # covered separately below -- so this checks the launch BAND hook only.)
    assert f'data-launch="launch-{pid}"' not in html
    store.close()


def test_compose_run_now_has_its_own_selects_when_a_handoff_is_queued(tmp_path):
    """Task 5 fix round 2 (IMPORTANT regression): the compose box's Run-now
    button used to point `data-compose-launch` at `launch-<project_id>` --
    the launch band's own id. That worked by accident before fix round 1
    (every stacked band shared that same id), but once bands were correctly
    keyed off their own handoff id, a page with >=1 queued handoff has NO
    band left with a project-id-keyed `lid` at all -- so the compose box's
    `bridgeLaunchBody` lookup resolved to nothing and silently posted
    `model: null, effort: null, permission_mode: null`. The compose box now
    owns its own selects (keyed on its own `cid`) via the shared
    `launch_options` macro, so `data-compose-launch` always names a select
    that is actually present on the page."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    cid = f"compose-{pid}"

    assert f'data-compose-launch="{cid}"' in html
    assert f'data-launch-model="{cid}"' in html
    assert f'data-launch-effort="{cid}"' in html
    assert f'data-launch-perm="{cid}"' in html
    assert f'id="{cid}-model"' in html
    assert f'id="{cid}-effort"' in html
    assert f'id="{cid}-perm"' in html
    store.close()


def test_current_tab_textareas_are_balanced(tmp_path):
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    assert html.count("<textarea") == html.count("</textarea>")
    store.close()


def test_dismiss_handoff_hook_present_when_queued(tmp_path):
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    assert 'data-handoff-dismiss="h1"' in html
    store.close()


def test_starting_a_different_session_does_not_touch_the_queued_handoff_field(tmp_path):
    """The compose box's own prompt field is a SEPARATE element from the
    handoff's -- a distinct id, so typing into one can never clear or
    replace the other."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    compose_id = f"compose-{pid}"
    assert f'data-compose-prompt="{compose_id}"' in html
    assert 'data-prompt-handoff="h1"' in html
    assert compose_id != "h1"
    # The compose textarea starts empty; only the handoff's carries the
    # queued prompt text.
    assert "keep going" not in html.split(f'id="{compose_id}"', 1)[1].split(
        "</textarea>", 1
    )[0]
    store.close()


def test_empty_state_when_no_handoff_offers_start_session(tmp_path):
    c, store, pid = _client(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    assert "No queued handoff for this project." in html
    tag = html.split(f'data-handoff-empty="{pid}"', 1)[1].split(">", 1)[0]
    assert "hidden" not in tag
    assert "Start session" in html
    store.close()


def test_handoff_empty_state_is_hidden_when_a_handoff_is_queued(tmp_path):
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    assert "hidden" in html.split(f'data-handoff-empty="{pid}"', 1)[1].split(">", 1)[0]
    store.close()


def test_change_options_disclosure_wraps_the_launch_selects(tmp_path):
    """The advanced launch options (model/effort/permission) live behind the
    ONE labeled "Change options" disclosure when a handoff is queued -- not
    beside it, and not as a second, separately-toggled set of controls."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    lid = "launch-h1"

    assert 'class="launch__options"' in html
    start = html.index('class="launch__options"')
    end = html.index("</details>", start)
    disclosure = html[start:end]

    assert "Change options" in disclosure, "the disclosure's own label"
    assert f'data-launch-model="{lid}"' in disclosure
    assert f'data-launch-effort="{lid}"' in disclosure
    assert f'data-launch-perm="{lid}"' in disclosure
    store.close()


def test_edit_prompt_reveals_the_editable_handoff_textarea_behind_a_preview(tmp_path):
    """A short preview of the saved prompt is always visible; the full,
    editable textarea -- same `data-prompt-handoff` hook, same text -- sits
    behind the "Edit prompt" toggle rather than being dropped."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text

    assert 'class="handoff__preview"' in html
    preview_start = html.index('class="handoff__preview"')
    preview = html[preview_start:html.index("</p>", preview_start)]
    assert "keep going" in preview, "the preview shows a slice of the saved prompt"

    assert 'class="handoff__edit"' in html
    edit_start = html.index('class="handoff__edit"')
    edit_block = html[edit_start:html.index("</details>", edit_start)]
    assert "Edit prompt" in edit_block, "the disclosure's own label"
    assert 'data-prompt-handoff="h1"' in edit_block, (
        "the editable textarea is INSIDE the disclosure, not dropped"
    )
    assert "keep going" in edit_block, "the textarea carries the full saved prompt"
    store.close()


def test_edit_prompt_disclosure_survives_the_live_morph(tmp_path):
    """The 'Edit prompt' <details> open state is client-owned, but the server
    always renders it collapsed and the live-update morph strips any attribute
    the incoming fragment omits (morph.js syncAttrs). Without data-live-preserve
    ON THE DISCLOSURE, an SSE refresh silently re-collapses the panel while the
    user is reading/editing. The attribute makes the morph hand off the whole
    subtree, keeping `open` -- and the textarea -- intact."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text

    edit_start = html.index('class="handoff__edit"')
    details_tag = html[edit_start:html.index(">", edit_start)]
    assert "data-live-preserve" in details_tag, (
        "the edit disclosure must be preserved so the live morph cannot strip "
        "its open state and auto-collapse it mid-edit"
    )
    store.close()


def test_collapsible_launch_surfaces_survive_the_live_morph(tmp_path):
    """Same bug class as the handoff editor across the rest of the Current tab:
    the 'Start a different session' and 'Change options' disclosures and the
    schedule mini-form are all server-rendered collapsed, and the live morph
    strips their open state (details `open`, panel `hidden`, toggle
    `aria-expanded`). Each interactive node must carry data-live-preserve so an
    SSE refresh cannot snap it shut mid-use."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text

    for cls in ("compose__disclosure", "launch__options"):
        start = html.index(f'class="{cls}"')
        tag = html[html.rindex("<", 0, start):html.index(">", start)]
        assert "data-live-preserve" in tag, f"the {cls} disclosure must survive the morph"

    panel_start = html.index('class="schedule-form"')
    panel_tag = html[html.rindex("<", 0, panel_start):html.index(">", panel_start)]
    assert "data-live-preserve" in panel_tag, "the schedule mini-form panel must survive the morph"

    toggle_start = html.index("data-schedule-toggle")
    toggle_tag = html[html.rindex("<", 0, toggle_start):html.index(">", toggle_start)]
    assert "data-live-preserve" in toggle_tag, "the schedule toggle must keep its aria-expanded"
    store.close()


# --- Task 3.4: Sessions / Handoffs / Launches history tabs -------------------


def _client_with_history(tmp_path):
    cfg = load({"db_path": tmp_path / "history.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/history-ui", "history-ui-project")
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", title="Did the work",
        ended_at=_ended(5),
    ), pid)
    store.create_handoff(Handoff(
        id="h1", project_path="/p/history-ui", next_prompt="next", created_at=1,
    ), pid)
    store.create_launch(Launch(
        id="l1", project_id=pid, mode="terminal", prompt="go", launched_at=1,
        outcome="started",
    ))
    return TestClient(create_app(store, cfg)), store, pid


def test_each_history_tab_wraps_its_table_in_the_shared_scroll_region(tmp_path):
    c, store, pid = _client_with_history(tmp_path)
    expected_label = {
        "sessions": "Indexed sessions table",
        "handoffs": "Handoffs table",
        "launches": "Launches table",
    }
    for tab, label in expected_label.items():
        html = c.get(f"/project/{pid}?tab={tab}").text
        assert 'class="table-scroll" tabindex="0" role="region"' in html
        assert f'aria-label="{label}"' in html
    store.close()


def test_history_tabs_state_the_true_total_not_a_bare_cap(tmp_path):
    """The old flat "up to 50" disclosure is gone: each tab states the real
    total (the fixture seeds one of each), which is the structural half the
    cap-disclosure cheap win never covered."""
    c, store, pid = _client_with_history(tmp_path)
    for tab in ("sessions", "handoffs", "launches"):
        html = c.get(f"/project/{pid}?tab={tab}").text
        assert "Showing 1–1 of 1" in html
        assert "up to 50" not in html
    store.close()


def test_history_pager_offers_next_and_previous_across_a_full_window(tmp_path):
    """>page_size rows: page 0 offers Next and no Previous; page 1 shows the
    51st row with Previous and no Next. The `?page=` rides the same `?tab=`
    query string the route already reads."""
    cfg = load({"db_path": tmp_path / "pager.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/pager", "pager-project")
    for i in range(51):
        store.upsert_session(SessionRecord(
            session_id=f"s{i}", transcript_path=f"/t/s{i}", ended_at=_ended(60 - i),
        ), pid)
    c = TestClient(create_app(store, cfg))

    first = c.get(f"/project/{pid}?tab=sessions").text
    assert "Showing 1–50 of 51" in first
    # The pager carries the active (here default) sort so paging never resets it.
    assert f'href="/project/{pid}?tab=sessions&page=1&sort=ended&dir=desc">Next</a>' in first
    assert ">Previous</a>" not in first

    second = c.get(f"/project/{pid}?tab=sessions&page=1").text
    assert "Showing 51–51 of 51" in second
    assert f'href="/project/{pid}?tab=sessions&page=0&sort=ended&dir=desc">Previous</a>' in second
    assert ">Next</a>" not in second
    store.close()


def test_history_pager_survives_an_out_of_range_page(tmp_path):
    """A hand-typed `?page=99` never crashes or lies: the slice is empty, the
    status reads "0 of N", and Previous still works to walk back."""
    c, store, pid = _client_with_history(tmp_path)  # one session
    html = c.get(f"/project/{pid}?tab=sessions&page=9").text
    assert "0 of 1" in html
    assert f'href="/project/{pid}?tab=sessions&page=8&sort=ended&dir=desc">Previous</a>' in html
    assert ">Next</a>" not in html
    store.close()


def test_history_tabs_expose_a_sortable_accessible_affordance(tmp_path):
    """The P2 detail-table controls: every history tab now marks its sortable
    columns with `aria-sort` and a visible (non-hover) `.sortable` affordance."""
    c, store, pid = _client_with_history(tmp_path)
    for tab in ("sessions", "handoffs", "launches"):
        html = c.get(f"/project/{pid}?tab={tab}").text
        assert "aria-sort" in html
        assert "sortable" in html
    store.close()


def test_default_history_marks_the_sort_column_and_leaves_others_neutral(tmp_path):
    """The default sessions view marks its Ended column `aria-sort="descending"`
    and every other sortable column `aria-sort="none"` -- the discoverable,
    persistent sort state the affordance is for."""
    c, store, pid = _client_with_history(tmp_path)
    html = c.get(f"/project/{pid}?tab=sessions").text
    assert 'aria-sort="descending"' in html
    assert 'aria-sort="none"' in html
    store.close()


def test_sorting_reorders_rows_and_marks_the_active_header_end_to_end(tmp_path):
    """Through the route (exercising the `?dir=` alias): a model-asc sort marks
    the Model header ascending and reorders the rows opposite the ended default."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/sort", "sort")
    # `zzz` ended more recently (default-first) but has the later model name;
    # `aaa` ended earlier but sorts first by model ascending -- so a correct
    # model-asc sort must invert the default order.
    store.upsert_session(SessionRecord(
        session_id="zzz", transcript_path="/t/zzz", title="zzz-session",
        ended_at=_ended(2), model="claude-sonnet-5",
    ), pid)
    store.upsert_session(SessionRecord(
        session_id="aaa", transcript_path="/t/aaa", title="aaa-session",
        ended_at=_ended(9), model="claude-opus-5",
    ), pid)
    c = TestClient(create_app(store, cfg))

    html = c.get(f"/project/{pid}?tab=sessions&sort=model&dir=asc").text
    assert 'aria-sort="ascending"' in html
    assert html.index("aaa-session") < html.index("zzz-session")
    store.close()


def test_filtering_narrows_rows_and_marks_the_active_chip_end_to_end(tmp_path):
    """Through the route (exercising the `?filter=` alias): filtering launches by
    outcome narrows the rows and marks the chosen chip active."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/flt", "flt")
    store.create_launch(Launch(
        id="lstart", project_id=pid, mode="terminal", prompt="a",
        launched_at=100, outcome="started",
    ))
    store.create_launch(Launch(
        id="lerr", project_id=pid, mode="terminal", prompt="b",
        launched_at=200, outcome="error",
    ))
    c = TestClient(create_app(store, cfg))

    html = c.get(f"/project/{pid}?tab=launches&filter=error").text
    # The error chip is active; the started row is filtered out of the total.
    assert 'chip chip--active" href="/project/' in html
    assert "error (1)" in html
    assert "Showing 1–1 of 1" in html
    store.close()


def test_sessions_tab_threads_sort_direction_and_model_filter(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/sf", "sort-filter")
    store.upsert_session(SessionRecord(
        session_id="a", transcript_path="/t/a", ended_at=_ended(5),
        model="claude-opus-5",
    ), pid)
    store.upsert_session(SessionRecord(
        session_id="b", transcript_path="/t/b", ended_at=_ended(10),
        model="claude-sonnet-5",
    ), pid)

    m = build_workspace(
        store, cfg, pid, "sessions", sort="model", direction="asc",
        filter_value="claude-opus-5",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )
    assert m.sort == "model"
    assert m.sort_dir == "asc"
    assert m.filter_value == "claude-opus-5"
    assert [s["id"] for s in m.sessions] == ["a"]  # sonnet row filtered out
    assert m.history_total == 1  # the total reflects the active filter
    # Facets are over the whole set, never the filtered slice.
    assert ("claude-opus-5", 1) in m.filter_facets
    assert ("claude-sonnet-5", 1) in m.filter_facets
    store.close()


def test_unknown_sort_and_filter_normalize_to_the_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/norm", "norm")
    store.upsert_session(SessionRecord(
        session_id="a", transcript_path="/t/a", ended_at=_ended(5),
        model="claude-opus-5",
    ), pid)

    m = build_workspace(
        store, cfg, pid, "sessions", sort="bogus", direction="sideways",
        filter_value="no-such-model",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )
    assert m.sort == "ended"  # unknown key -> the table's default column
    assert m.sort_dir == "desc"  # anything but "asc" -> desc
    assert m.filter_value is None  # a filter naming no facet -> all
    assert m.history_total == 1  # so nothing is filtered out
    store.close()


def test_current_tab_exposes_no_sort_or_filter_state(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/cur", "cur")
    m = build_workspace(
        store, cfg, pid, "current",
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )
    assert m.sort == ""
    assert m.filter_value is None
    assert m.filter_facets == []
    store.close()


def test_history_tables_use_tabular_numerals():
    css = (
        Path(__file__).resolve().parent.parent
        / "src" / "bridge" / "static" / "app.css"
    ).read_text()
    assert (
        ".handoffs th, .handoffs td, .launches th, .launches td {\n"
        "  text-align: left;\n"
        "  padding: .4rem .5rem;\n"
        "  border-bottom: 1px solid var(--row-border);\n"
        "  font-size: .85rem;\n"
        "  vertical-align: top;\n"
        "  font-variant-numeric: tabular-nums;\n"
        "}"
    ) in css
    assert ".sessions td { font-variant-numeric: tabular-nums; }" in css
