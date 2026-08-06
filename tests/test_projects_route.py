"""Projects index: read model + route.

`build_projects` reuses `bridge.cards.build_cards` -- the same probe pattern
`build_overview` uses -- and `bridge.overview.project_summary`, promoted from
a private helper so Overview and Projects render identical rows off one
projection instead of two that could drift. The route tests assert what a
browser actually receives: a labelled search field, the five filter controls
with their counts, exactly one `Open project` link per active project, no
`<textarea>`/`data-launch-model` (that surface belongs to the Project
workspace, not this index), branch/dirty and last-session age on the row, and
the Pin/Hide/Restore hooks `projects.js` already delegates on.
"""

import re
import subprocess
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from bridge.agents import AgentsState, LiveSession
from bridge.api import create_app
from bridge.config import load
from bridge.models import GitState, Handoff, SessionRecord
from bridge.projects_view import build_projects
from bridge.store import Store

GIT = "/usr/bin/git"


def _cfg(tmp_path, name="projects"):
    return load({"db_path": tmp_path / f"{name}.db", "spool_dir": tmp_path / "spool"})


def _ended(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _git(cwd, *args):
    subprocess.run([GIT, *args], cwd=cwd, check=True, capture_output=True, text=True)


def _dirty_repo(tmp_path):
    """A real repo with one uncommitted change, so the git probe `build_cards`
    runs for real (not injected) still has something to report."""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.name", "t")
    _git(d, "config", "user.email", "t@t")
    (d / "a.txt").write_text("hello\n")
    _git(d, "add", "a.txt")
    _git(d, "commit", "-q", "-m", "first commit")
    (d / "a.txt").write_text("changed\n")
    return d


# --- Model: build_projects ---------------------------------------------------


def test_build_projects_counts_by_status(tmp_path):
    """Seeds a queued-handoff project, a running one, a stale one, an idle one,
    and a hidden one -- and asserts every count on `ProjectsModel.counts`."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000

    queued_id = store.upsert_project("/p/queued", "queued-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/queued", next_prompt="keep going", created_at=now,
    ), queued_id)

    running_id = store.upsert_project("/p/running", "running-project")
    stale_id = store.upsert_project("/p/stale", "stale-project")
    idle_id = store.upsert_project("/p/idle", "idle-project")

    hidden_id = store.upsert_project("/p/hidden", "hidden-project")
    store.set_project_status(hidden_id, "hidden")

    def probe(path: str) -> GitState:
        if path == "/p/stale":
            return GitState(
                status="ok", branch="main", dirty_count=2,
                oldest_uncommitted_at=now - 100 * 3600,
            )
        return GitState(status="ok", branch="main", dirty_count=0)

    def agents_fn() -> AgentsState:
        return AgentsState(status="ok", sessions=[LiveSession(
            session_id="live-1", cwd="/p/running", kind="interactive", status="busy",
        )])

    model = build_projects(store, cfg, probe_fn=probe, agents_fn=agents_fn)

    # Hidden never reaches `build_cards` at all (`store.projects()`
    # whitelists `active`), so `all` counts only the four active projects.
    assert model.counts["all"] == 4
    assert model.counts["needs_attention"] == 3  # queued + running + stale
    assert model.counts["running"] == 1
    assert model.counts["queued"] == 1
    assert model.counts["hidden"] == 1
    assert [p["id"] for p in model.hidden] == [hidden_id]
    assert {row.project_id for row in model.rows} == {
        queued_id, running_id, stale_id, idle_id,
    }

    store.close()


def test_queued_count_counts_handoffs_not_cards(tmp_path):
    """Two queued handoffs on one project count as 2, not 1 -- the `queued`
    count used to be "cards with a handoff" and silently hid a second queued
    handoff on the same project."""
    cfg = _cfg(tmp_path, "queued-count")
    store = Store(cfg.db_path)
    now = 10_000

    pid = store.upsert_project("/proj/a", "project-a")
    store.create_handoff(Handoff(
        id="h1", project_path="/proj/a", next_prompt="plan", source_session_id="s1",
        created_at=now,
    ), pid)
    store.create_handoff(Handoff(
        id="h2", project_path="/proj/a", next_prompt="ui", source_session_id="s2",
        created_at=now,
    ), pid)

    model = build_projects(
        store, cfg,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert model.counts["queued"] == 2
    store.close()


def test_running_count_excludes_a_queued_and_running_project(tmp_path):
    """A project with BOTH a queued handoff and a live session renders as
    "queued" (a queued handoff outranks a running session -- see
    overview._status_word), and the Running filter matches on that rendered
    state. So the Running count must not include it either: counting it under
    Running while the row shows "queued" and the Running filter hides it is the
    badge/count-vs-filter mismatch this guards against. It is still counted
    under Queued (its handoff), consistent with how it renders."""
    cfg = _cfg(tmp_path, "queued-and-running")
    store = Store(cfg.db_path)
    now = 10_000

    pid = store.upsert_project("/p/both", "both-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/both", next_prompt="keep going", created_at=now,
    ), pid)

    def agents_fn() -> AgentsState:
        return AgentsState(status="ok", sessions=[LiveSession(
            session_id="live-1", cwd="/p/both", kind="interactive", status="busy",
        )])

    model = build_projects(
        store, cfg,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=agents_fn,
    )

    assert model.rows[0].status_word == "queued"
    assert model.counts["running"] == 0
    assert model.counts["queued"] == 1
    assert model.counts["needs_attention"] == 1
    store.close()


def test_build_projects_rows_keep_the_card_actionability_order(tmp_path):
    """`rows` is `[project_summary(card, now) for card in cards]` -- the exact
    order `build_cards`/`cards.sort_key` already produces (handoff first),
    never re-sorted here."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    now = 10_000

    quiet_id = store.upsert_project("/p/quiet", "quiet-project")
    queued_id = store.upsert_project("/p/queued", "queued-project")
    store.create_handoff(Handoff(
        id="h1", project_path="/p/queued", next_prompt="keep going", created_at=now,
    ), queued_id)

    model = build_projects(
        store, cfg,
        probe_fn=lambda path: GitState(status="ok", branch="main"),
        agents_fn=lambda: AgentsState(status="ok", sessions=[]),
    )

    assert [row.project_id for row in model.rows] == [queued_id, quiet_id]
    store.close()


# --- Route: GET /projects ----------------------------------------------------


def test_get_projects_returns_200_with_search_filters_and_rows(tmp_path):
    cfg = _cfg(tmp_path, "route")
    store = Store(cfg.db_path)
    repo = _dirty_repo(tmp_path)
    pid = store.upsert_project(str(repo), "demo-project")
    store.upsert_session(SessionRecord(
        session_id="s1", transcript_path="/t/s1", title="Did the thing",
        ended_at=_ended(5),
    ), pid)

    hidden_id = store.upsert_project("/p/tucked-away", "tucked-away")
    store.set_project_status(hidden_id, "hidden")
    store.close()

    store2 = Store(cfg.db_path)
    client = TestClient(create_app(store2, cfg))
    resp = client.get("/projects")
    assert resp.status_code == 200
    html = resp.text

    # A labelled search field.
    assert "Search projects" in html
    assert 'for="projects-search"' in html
    assert "data-projects-search" in html

    # Five filter controls, plus a live result count.
    for value in ("all", "needs_attention", "running", "queued", "hidden"):
        assert f'data-projects-filter="{value}"' in html
    assert "data-projects-count" in html

    # One `Open project` row action per active project (the hidden one is not
    # a card and so gets no row action of its own -- just its Restore hook).
    assert html.count("Open project") == 1
    assert f'href="/project/{pid}"' in html

    # No launch/compose surface on this page -- that lives on the workspace.
    assert html.count("<textarea") == 0
    assert "data-launch-model" not in html
    assert "data-compose-prompt" not in html

    # Branch/dirty summary and last-session age.
    assert re.search(r"\bmain\b", html)
    assert "dirty" in html
    assert "Did the thing" in html
    assert "ago" in html

    # Pin/Hide/Restore, reusing the existing hooks.
    assert f'data-project-pin="{pid}"' in html
    assert f'data-project-hide="{pid}"' in html
    assert f'data-project-restore="{hidden_id}"' in html

    # The hidden entry is a nav dead-end if linked: the workspace route 404s
    # for hidden projects by design. So its name is plain text, never a link,
    # and Restore is its only action.
    assert f'href="/project/{hidden_id}"' not in html
    assert "tucked-away" in html

    # Exactly one `<h1>`, and the nav marks Projects active.
    assert len(re.findall(r"<h1\b", html)) == 1
    assert re.search(r'href="/projects"[^>]*aria-current="page"', html) or \
        re.search(r'aria-current="page"[^>]*href="/projects"', html)

    store2.close()


def test_projects_index_offers_a_labelled_list_and_grid_toggle(tmp_path):
    """Both layout buttons carry a visible WORD, not just a glyph -- an
    unlabelled icon pair is the mystery-meat control the nav rail already
    refuses to ship -- and the glyphs are `aria-hidden` so the accessible name
    stays the word. Server-renders List pressed; projects.js corrects it from
    the stored preference on load."""
    cfg = _cfg(tmp_path, "views")
    store = Store(cfg.db_path)
    client = TestClient(create_app(store, cfg))
    html = client.get("/projects").text

    for value, word in (("list", "List"), ("grid", "Grid")):
        match = re.search(
            rf'<button[^>]*data-projects-view-button="{value}".*?</button>', html, re.S
        )
        assert match, f"no {value} layout button"
        button = match.group(0)
        # Strip the icon, then all remaining tags: what is left is what a
        # screen reader gets. Asserting on the stripped text rather than on a
        # literal `>List<` keeps this from breaking every time the icon markup
        # is reformatted -- which is exactly how it broke once already.
        without_icon = re.sub(r"<svg.*?</svg>", "", button, flags=re.S)
        assert re.sub(r"<[^>]+>", "", without_icon).strip() == word
        # The icon must not reach the accessible name, or it would read twice.
        assert 'aria-hidden="true"' in button

    # Exactly one pressed on the server render, and it is List.
    group = html[html.index('class="projects-views"'):html.index("projects-count")]
    assert group.count('aria-pressed="true"') == 1
    assert 'data-projects-view-button="list"' in group
    store.close()


def test_projects_index_shows_an_empty_state_with_no_projects(tmp_path):
    cfg = _cfg(tmp_path, "empty")
    store = Store(cfg.db_path)
    client = TestClient(create_app(store, cfg))
    html = client.get("/projects").text
    assert 'class="empty"' in html
    assert html.count("Open project") == 0
    store.close()


def test_projects_index_rows_separate_identity_activity_and_action(tmp_path):
    """The index should scan like a project directory, not a run-on flex row.
    Identity leads, activity follows, and the one primary row action trails."""
    cfg = _cfg(tmp_path, "row-composition")
    store = Store(cfg.db_path)
    repo = _dirty_repo(tmp_path)
    pid = store.upsert_project(str(repo), "demo-project")
    client = TestClient(create_app(store, cfg))

    html = client.get("/projects").text
    identity = html.index('class="project-row__identity"')
    activity = html.index('class="project-row__activity"')
    action = html.index('class="project-row__action"')

    assert identity < activity < action
    assert f'href="/project/{pid}"' in html[action:]
    store.close()


def test_projects_index_action_disclosure_is_constant_width_but_still_named(tmp_path):
    """The `<summary>` used to render `Actions for {name}`, so the disclosure
    column resized per project and dragged the whole trailing action column
    with it. The visible text is now the constant `Actions`; the project name
    moves to `aria-label` so each of the 36 disclosures still has a distinct
    accessible name in a screen reader's rotor."""
    cfg = _cfg(tmp_path, "action-column")
    store = Store(cfg.db_path)
    repo = _dirty_repo(tmp_path)
    store.upsert_project(str(repo), "demo-project")
    client = TestClient(create_app(store, cfg))

    html = client.get("/projects").text
    assert '<summary aria-label="Actions for demo-project">Actions</summary>' in html
    # The name must be gone from the *rendered* text, not merely relocated.
    assert ">Actions for demo-project<" not in html

    # `projects-index` is the CSS scope every rule in this pass hangs off (now
    # the group wrapper, with each collapsible group holding a plain
    # `projects-list`), and the Overview's own list must never grow it.
    assert 'class="projects-index"' in html
    assert '<ul class="projects-list">' in html
    assert "projects-index" not in client.get("/").text
    store.close()


def test_projects_index_column_header_band_is_presentational(tmp_path):
    """The band labels the row tracks, but the thing below it is a `<ul>`, not
    a grid -- announcing "Project, Status and activity" would promise a
    structure that is not there. So it is `aria-hidden`, and its labels are
    authored in sentence case (CSS renders the small caps) rather than typed
    in capitals."""
    cfg = _cfg(tmp_path, "header-band")
    store = Store(cfg.db_path)
    html = TestClient(create_app(store, cfg)).get("/projects").text
    assert '<div class="projects-index-head" aria-hidden="true">' in html
    assert ">Project<" in html and ">Status &amp; activity<" in html
    assert "STATUS &amp; ACTIVITY" not in html
    store.close()


def test_projects_index_rows_carry_the_state_hook_and_group_by_status(tmp_path):
    """State now lives in two places that must agree: every row keeps its
    `[data-project-state]` hook (the interface projects.js filters on), and the
    row sits inside a collapsible group whose `[data-project-group]` header
    carries the humanized label and the one dot of colour. The per-row pill is
    gone -- the group header is the status cue -- so a `pill--` class must not
    appear inside the ledger any more."""
    cfg = _cfg(tmp_path, "state-hook")
    store = Store(cfg.db_path)

    queued_id = store.upsert_project(str(_dirty_repo(tmp_path)), "queued-project")
    store.create_handoff(Handoff(
        id="h1", project_path=str(tmp_path / "repo"), next_prompt="keep going",
        created_at=int(datetime.now(timezone.utc).timestamp()),
    ), queued_id)
    store.upsert_project("/p/quiet", "quiet-project")

    html = TestClient(create_app(store, cfg)).get("/projects").text

    # The row hook still spells the raw status word (queued handoff -> queued;
    # a path with no repo and no session -> idle).
    assert 'data-project-state="queued"' in html
    assert 'data-project-state="idle"' in html

    # Each row sits in a group whose header names the state in words. The
    # header is the redundant non-colour cue (WCAG 1.4.1) beside the dot.
    assert '<details class="projects-group" data-project-group="queued"' in html
    assert '<details class="projects-group" data-project-group="idle"' in html
    assert ">Queued</span>" in html and ">Idle</span>" in html

    # The per-row status pill is gone from the ledger now that the group owns
    # the state cue.
    assert "pill pill--" not in html
    store.close()


def test_status_label_renames_stale_to_uncommitted():
    """`stale` is the state Mit flagged as opaque -- it means uncommitted work
    sitting past `stale_hours`, so no user-facing surface may say the raw word.
    The enum stays `stale` (JS/CSS/mutation anchors key off it); only the label
    changes."""
    from bridge.projects_view import status_label

    assert status_label("stale") == "Uncommitted"
    assert status_label("running") == "Running"
    assert status_label("queued") == "Queued"
    assert status_label("recent") == "Recent"
    assert status_label("idle") == "Idle"


def test_group_projects_buckets_pinned_first_then_by_status(tmp_path):
    """Groups render in a fixed order (Pinned, Running, Queued, Uncommitted,
    Recent, Idle), a pinned project is pulled out regardless of its status, and
    empty groups drop out. Rows keep their incoming sort order within a group."""
    from bridge.overview import ProjectSummary
    from bridge.projects_view import group_projects

    def _summary(name, status, pinned=False):
        return ProjectSummary(
            project_id=hash(name) & 0xFFFF, name=name, path=f"/p/{name}",
            status_word=status, branch=None, dirty_count=0,
            last_session_title=None, last_session_age_seconds=None,
            tokens_today=0, tokens_5h=0, pinned=pinned, ahead=None, behind=None,
        )

    rows = [
        _summary("pinned-idle", "idle", pinned=True),
        _summary("run-a", "running"),
        _summary("queue-a", "queued"),
        _summary("stale-a", "stale"),
    ]
    groups = group_projects(rows)
    assert [g.key for g in groups] == ["pinned", "running", "queued", "stale"]
    assert [g.label for g in groups] == ["Pinned", "Running", "Queued", "Uncommitted"]
    assert groups[0].rows[0].name == "pinned-idle"
    # No Recent/Idle group: the only idle project was pinned out of it.
    assert "idle" not in {g.key for g in groups}


def test_overview_nav_now_links_to_projects(tmp_path):
    """Projects is functional as of this task, so the shared shell's nav must
    grow to include it (no dead ends) rather than staying Milestone 1's
    Overview/Diagnostics-only list."""
    cfg = _cfg(tmp_path, "nav")
    store = Store(cfg.db_path)
    client = TestClient(create_app(store, cfg))
    html = client.get("/").text
    assert 'href="/projects"' in html
    store.close()
