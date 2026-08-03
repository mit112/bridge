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


def test_history_tabs_never_carry_the_current_tabs_workspace_state_panel(tmp_path):
    c, store, pid = _client(tmp_path)
    for tab in ("sessions", "handoffs", "launches"):
        html = c.get(f"/project/{pid}?tab={tab}").text
        assert 'class="workspace-state"' not in html
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


def test_current_tab_exposes_interactive_hooks_keyed_off_the_project_id(tmp_path):
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    lid = f"launch-{pid}"
    assert f'data-launch="{lid}"' in html
    assert f'data-launch-model="{lid}"' in html
    assert f'data-launch-perm="{lid}"' in html
    assert f'data-launch-button="{lid}"' in html
    assert 'data-prompt-handoff="h1"' in html
    store.close()


def test_permission_select_defaults_to_the_no_flag_option_not_a_suggestion(tmp_path):
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    lid = f"launch-{pid}"
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
    lid = f"launch-{pid}"
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


def test_queued_handoff_shows_its_age_beside_the_summary(tmp_path):
    """Spec line 180: the queued handoff shows its summary AND its age, so a
    stale next step reads as stale. `created_at` is on every handoff row."""
    c, store, pid = _client_with_handoff(tmp_path)
    html = c.get(f"/project/{pid}?tab=current").text
    summary = html.split('class="handoff__summary"', 1)[1].split("</p>", 1)[0]
    assert "finish the thing" in summary, "the summary still renders"
    assert 'class="handoff__age' in summary, "the age sits beside the summary"
    assert "queued" in summary and "ago" in summary
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
    assert "No session in progress for this project." in html
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
    lid = f"launch-{pid}"

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


def test_history_tabs_state_the_50_record_cap_explicitly(tmp_path):
    c, store, pid = _client_with_history(tmp_path)
    for tab in ("sessions", "handoffs", "launches"):
        html = c.get(f"/project/{pid}?tab={tab}").text
        assert "Showing up to 50 most recent records." in html
    store.close()


def test_history_tabs_offer_no_sortable_affordance(tmp_path):
    c, store, pid = _client_with_history(tmp_path)
    for tab in ("sessions", "handoffs", "launches"):
        html = c.get(f"/project/{pid}?tab={tab}").text
        assert "aria-sort" not in html
        assert "sortable" not in html
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
