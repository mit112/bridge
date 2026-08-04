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

    # `projects-index` is the CSS scope every rule in this pass hangs off, and
    # the Overview's own list must never grow it (see app.css's Almanac block).
    assert 'class="projects-list projects-index"' in html
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


def test_projects_index_rows_carry_the_state_hook_the_status_edge_reads(tmp_path):
    """The status edge is pure CSS keyed on `[data-project-state]`, so these
    four literal strings are an interface, not an implementation detail --
    renaming one in the template would silently drop a row's colour with no
    other test noticing. The word beside it is the redundant, non-colour cue
    (WCAG 1.4.1), so the two must always agree."""
    cfg = _cfg(tmp_path, "state-hook")
    store = Store(cfg.db_path)

    queued_id = store.upsert_project(str(_dirty_repo(tmp_path)), "queued-project")
    store.create_handoff(Handoff(
        id="h1", project_path=str(tmp_path / "repo"), next_prompt="keep going",
        created_at=int(datetime.now(timezone.utc).timestamp()),
    ), queued_id)
    store.upsert_project("/p/quiet", "quiet-project")

    html = TestClient(create_app(store, cfg)).get("/projects").text
    pairs = re.findall(
        r'data-project-state="([a-z]+)".*?<span class="pill pill--([a-z]+)">([a-z]+)</span>',
        html, re.S,
    )
    assert len(pairs) == 2
    # `idle` is the one state that covers two words -- `_status_word` says
    # "recent" for a project with a past session and "idle" for one with none
    # -- and it is also the only state the edge deliberately leaves
    # uncoloured, so both words must land in that bucket.
    words = {
        "queued": {"queued"}, "running": {"running"},
        "stale": {"stale"}, "idle": {"recent", "idle"},
    }
    for state, pill_class, word in pairs:
        assert state in words, f"unknown state {state!r} has no status edge"
        assert word in words[state]
        assert pill_class == word
    assert "queued" in {state for state, _, _ in pairs}
    store.close()


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
