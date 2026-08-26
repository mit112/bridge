import dataclasses
import logging
from pathlib import Path

import pytest

from bridge import models
from bridge.config import load
from bridge.indexer import reindex
from bridge.registry import transcript_files
from bridge.store import Store
from tests.conftest import jline, launch_by_session

SID = "22222222-2222-2222-2222-222222222222"


_ILLUSTRATIVE_ROOTS = (
    Path("/Users/you/dev"),
    Path("/Users/you/Documents"),
)


@pytest.fixture(autouse=True)
def legacy_fixture_cwds_are_not_this_tasks_concern(monkeypatch):
    """Task 1 adds an auto-archive pass that calls `Path.exists()` on every
    project row. Nearly every fixture in this module attributes sessions to
    illustrative absolute paths under `/Users/you/dev/` and
    `/Users/you/Documents/` (e.g. `/Users/you/dev/demo`) that were
    never meant to be real directories, and rewriting every such literal in
    this file to live under `tmp_path` is out of proportion to this task.

    The fake reports ONLY those two illustrative roots (and paths under them)
    as present -- restoring this module's pre-Task-1 behavior for them -- and
    calls the real `Path.exists` for everything else. That includes every
    `tmp_path`-based path (this task's own vanished/still-here tests), and
    critically the real `~/.bridge/config.toml` probe that
    `test_against_the_real_corpus_...` deliberately exercises: faking that
    probe to always say "present" would make the test's `delenv` of
    `BRIDGE_CONFIG` silently stop testing what it claims to, on whatever
    machine happens to lack that file.

    A future test asserting auto-archival of a project literally under one of
    these two roots would see it "exist" here and never archive: build such
    paths under `tmp_path` instead.
    """
    real_exists = Path.exists

    def fake_exists(self):
        if self in _ILLUSTRATIVE_ROOTS or any(
            root in self.parents for root in _ILLUSTRATIVE_ROOTS
        ):
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)


def transcript_lines(sid=SID, cwd="/Users/you/dev/demo", title="Did work"):
    return [
        jline(type="user", sessionId=sid, isSidechain=False,
              timestamp="2026-07-30T10:00:00.000Z", cwd=cwd, gitBranch="main",
              message={"role": "user", "content": "go"}),
        jline(type="assistant", sessionId=sid, isSidechain=False,
              timestamp="2026-07-30T10:05:00.000Z", cwd=cwd, effort="high",
              message={"role": "assistant", "model": "claude-opus-5",
                       "usage": {"input_tokens": 1, "output_tokens": 2}}),
        jline(type="ai-title", sessionId=sid, aiTitle=title),
    ]


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "projects"
    (projects / "-Users-you-dev-demo").mkdir(parents=True)
    # `spool_dir` and `launches_dir` are overridden even though indexing writes
    # to neither: conftest's guard only fires on a call, so an un-overridden
    # Config here is a trap for the next test added to this module. `discovery_paths`
    # is overridden too, and to a directory that does not exist: left at its
    # default (the real `~/dev`), Task 2's discovery pass would card every git
    # repo actually checked out there (this repo included) into every test in
    # this module that does not otherwise care about dev-repo discovery.
    cfg = load({
        "claude_projects_dir": projects,
        "db_path": tmp_path / "b.db",
        "spool_dir": tmp_path / "spool",
        "launches_dir": tmp_path / "launches",
        "discovery_paths": (tmp_path / "dev",),
    })
    store = Store(cfg.db_path)
    yield cfg, store, projects
    store.close()


def write(projects, name, lines, dirname="-Users-you-dev-demo"):
    p = projects / dirname / name
    p.write_text("".join(lines))
    return p


def test_reindex_archives_a_project_whose_path_has_vanished(env, tmp_path):
    cfg, store, _ = env
    pid = store.upsert_project(str(tmp_path / "deleted-project"), "deleted-project")
    reindex(store, cfg)
    row = store.get_project(pid)
    assert row["status"] == "archived"
    assert row["missing_archived_at"] is not None


def test_reindex_leaves_a_project_whose_path_still_exists_alone(env, tmp_path):
    cfg, store, _ = env
    here = tmp_path / "still-here"
    here.mkdir()
    pid = store.upsert_project(str(here), "still-here")
    reindex(store, cfg)
    row = store.get_project(pid)
    assert row["status"] == "active"
    assert row["missing_archived_at"] is None


def test_a_restored_project_is_not_re_archived_even_though_it_is_still_gone(env, tmp_path):
    """The seed-vs-override rule: config seeds once, the panel overrides. A
    manual restore of a still-missing project must survive the next index."""
    cfg, store, _ = env
    pid = store.upsert_project(str(tmp_path / "deleted-project"), "deleted-project")
    reindex(store, cfg)                        # archives + stamps
    store.set_project_status(pid, "active")    # user restores in the panel
    reindex(store, cfg)                        # must NOT re-archive
    assert store.get_project(pid)["status"] == "active"


def test_reindex_cards_a_dev_repo_that_has_no_transcripts(env, tmp_path):
    cfg, store, _ = env
    dev = tmp_path / "dev"
    (dev / "lonely-repo" / ".git").mkdir(parents=True)
    (dev / "not-a-repo").mkdir()
    reindex(store, dataclasses.replace(cfg, discovery_paths=(dev,)))
    paths = {r["path"] for r in store.projects()}
    assert str(dev / "lonely-repo") in paths, "a transcript-less git repo gets an active card"
    assert str(dev / "not-a-repo") not in paths, "a plain dir is not a project"


def test_reindex_discovery_does_not_unarchive_a_hidden_dev_repo(env, tmp_path):
    cfg, store, _ = env
    dev = tmp_path / "dev"
    (dev / "muted-repo" / ".git").mkdir(parents=True)
    cfg2 = dataclasses.replace(cfg, discovery_paths=(dev,))
    reindex(store, cfg2)                                   # creates the row
    pid = store.project_by_path(str(dev / "muted-repo"))["id"]
    store.set_project_status(pid, "hidden")               # user mutes it
    reindex(store, cfg2)                                  # must stay hidden
    assert store.get_project(pid)["status"] == "hidden"


def test_first_index_creates_project_and_session(env):
    cfg, store, projects = env
    write(projects, "s.jsonl", transcript_lines())
    stats = reindex(store, cfg)
    assert stats.files_scanned == 1
    assert stats.sessions_upserted == 1
    projs = store.projects()
    assert len(projs) == 1
    assert projs[0]["path"] == "/Users/you/dev/demo"
    assert projs[0]["name"] == "demo"
    assert store.latest_session(projs[0]["id"])["title"] == "Did work"


def test_unchanged_file_is_not_rescanned(env):
    cfg, store, projects = env
    write(projects, "s.jsonl", transcript_lines())
    reindex(store, cfg)
    second = reindex(store, cfg)
    assert second.files_seen == 1
    assert second.files_scanned == 0
    assert second.lines_parsed == 0


def test_appended_file_scans_only_the_delta(env):
    cfg, store, projects = env
    p = write(projects, "s.jsonl", transcript_lines())
    reindex(store, cfg)
    with p.open("a") as f:
        f.write(jline(type="ai-title", sessionId=SID, aiTitle="Renamed"))
    stats = reindex(store, cfg)
    assert stats.files_scanned == 1
    assert stats.lines_parsed == 1
    pid = store.projects()[0]["id"]
    assert store.latest_session(pid)["title"] == "Renamed"


def test_shrunk_file_is_rescanned_from_zero(env):
    cfg, store, projects = env
    p = write(projects, "s.jsonl", transcript_lines())
    reindex(store, cfg)
    # Title must be shorter than "Did work" so the file genuinely shrinks in
    # bytes and exercises the shrink-detection path (not just a same-size
    # rewrite), which is the real-world rewrite case this test targets.
    p.write_text("".join(transcript_lines(title="X")))
    stats = reindex(store, cfg)
    assert stats.files_scanned == 1
    pid = store.projects()[0]["id"]
    assert store.latest_session(pid)["title"] == "X"


def test_reindex_is_idempotent(env):
    cfg, store, projects = env
    write(projects, "s.jsonl", transcript_lines())
    reindex(store, cfg)
    reindex(store, cfg)
    reindex(store, cfg)
    pid = store.projects()[0]["id"]
    assert len(store.sessions(pid)) == 1


def test_record_without_cwd_is_skipped_not_fatal(env):
    cfg, store, projects = env
    write(projects, "nocwd.jsonl", [jline(type="assistant", sessionId="zz")])
    write(projects, "ok.jsonl", transcript_lines())
    stats = reindex(store, cfg)
    assert stats.sessions_upserted == 1  # only the one with a resolvable project
    assert len(store.projects()) == 1


def test_malformed_file_does_not_abort_the_run(env):
    cfg, store, projects = env
    write(projects, "bad.jsonl", ["{broken\n"])
    write(projects, "ok.jsonl", transcript_lines())
    stats = reindex(store, cfg)
    assert stats.parse_errors >= 1
    assert stats.sessions_upserted == 1


def test_a_failed_diagnostics_record_is_logged_not_silently_swallowed(
    env, monkeypatch, caplog
):
    """A failed `record_index_run` blinds Diagnostics freshness; the log is the
    only signal, since the scan itself must still succeed."""
    cfg, store, projects = env
    write(projects, "ok.jsonl", transcript_lines())

    def boom(*args, **kwargs):
        raise RuntimeError("diagnostics table is locked")

    monkeypatch.setattr(store, "record_index_run", boom)

    with caplog.at_level(logging.WARNING, logger="bridge.indexer"):
        stats = reindex(store, cfg)

    # Behaviour preserved: the scan completed and returned its stats.
    assert stats.sessions_upserted == 1
    assert len(store.projects()) == 1
    # ...and the operator now gets a warning instead of silence.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("index run" in r.getMessage() for r in warnings)


def test_two_projects_are_separated(env):
    cfg, store, projects = env
    (projects / "-Users-you-dev-other").mkdir()
    write(projects, "a.jsonl", transcript_lines())
    write(projects, "b.jsonl",
          transcript_lines(sid="33333333-3333-3333-3333-333333333333",
                           cwd="/Users/you/dev/other"),
          dirname="-Users-you-dev-other")
    reindex(store, cfg)
    assert {p["name"] for p in store.projects()} == {"demo", "other"}


# --- Path aliasing -----------------------------------------------------------
#
# Old `~/Documents/...` cwds and their `~/dev/...` successors are the same
# logical project. `aliased_env` models that with one alias, one unrelated
# project, and one archived path.

OLD = "/Users/you/Documents/demo"
NEW = "/Users/you/dev/demo"
GONE = "/Users/you/Documents/deleted-thing"
OLD_DIR = "-Users-you-Documents-demo"
GONE_DIR = "-Users-you-Documents-deleted-thing"
SID_B = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def aliased_env(tmp_path):
    projects = tmp_path / "projects"
    for d in ("-Users-you-dev-demo", OLD_DIR, GONE_DIR,
              "-Users-you-dev-other"):
        (projects / d).mkdir(parents=True)
    cfg = load({
        "claude_projects_dir": projects,
        "db_path": tmp_path / "b.db",
        "aliases": {OLD: NEW},
        "archived_paths": (GONE,),
        # See `env`'s fixture comment: without this, discovery would card the
        # real `~/dev` into every test that uses this fixture.
        "discovery_paths": (tmp_path / "dev",),
    })
    store = Store(cfg.db_path)
    yield cfg, store, projects
    store.close()


def test_session_recorded_under_an_alias_attributes_to_the_canonical_path(aliased_env):
    cfg, store, projects = aliased_env
    write(projects, "old.jsonl", transcript_lines(cwd=OLD), dirname=OLD_DIR)
    reindex(store, cfg)
    assert [p["path"] for p in store.projects()] == [NEW]


def test_split_history_across_an_alias_merges_into_one_project(aliased_env):
    """The point of the feature: sessions from before and after the move land
    in a single card with a single history."""
    cfg, store, projects = aliased_env
    write(projects, "old.jsonl", transcript_lines(cwd=OLD), dirname=OLD_DIR)
    write(projects, "new.jsonl", transcript_lines(sid=SID_B, cwd=NEW))
    reindex(store, cfg)
    projs = store.projects()
    assert len(projs) == 1
    assert projs[0]["path"] == NEW
    assert {s["id"] for s in store.sessions(projs[0]["id"])} == {SID, SID_B}


def test_a_path_with_no_alias_is_attributed_unchanged(aliased_env):
    cfg, store, projects = aliased_env
    write(projects, "o.jsonl", transcript_lines(cwd="/Users/you/dev/other"),
          dirname="-Users-you-dev-other")
    reindex(store, cfg)
    assert [p["path"] for p in store.projects()] == ["/Users/you/dev/other"]


def test_configured_archive_path_is_created_then_hidden(aliased_env):
    """The project row does not exist until this run creates it, so archiving
    must happen after indexing, not before."""
    cfg, store, projects = aliased_env
    write(projects, "g.jsonl", transcript_lines(cwd=GONE), dirname=GONE_DIR)
    reindex(store, cfg)
    assert store.projects() == []
    hidden = store.projects(include_hidden=True)
    assert [(p["path"], p["status"]) for p in hidden] == [(GONE, "archived")]


def test_aliases_from_config_are_persisted_to_the_alias_table(aliased_env):
    """Seeded into the DB, not just applied in memory, so a future UI-added
    alias and a config-declared one live in the same place."""
    cfg, store, projects = aliased_env
    reindex(store, cfg)
    assert store.alias_map() == {OLD: NEW}


def test_restoring_an_archived_project_survives_the_next_index(aliased_env):
    """Config seeds; the database overrides.

    `config.toml` still lists this path, so a run that re-asserted the config
    would silently undo the restore at the next index — config overriding the
    user rather than seeding them, and no way to tell from the panel why the
    project kept disappearing.
    """
    cfg, store, projects = aliased_env
    write(projects, "g.jsonl", transcript_lines(cwd=GONE), dirname=GONE_DIR)
    reindex(store, cfg)
    row = store.project_by_path(GONE)
    assert row["status"] == "archived"

    store.set_project_status(row["id"], "active")
    reindex(store, cfg)

    assert store.project_by_path(GONE)["status"] == "active"
    assert [p["path"] for p in store.projects()] == [GONE]


def test_a_newly_configured_archive_path_is_still_seeded_on_a_later_run(aliased_env):
    """Seed-on-first-sight, not seed-once-ever.

    A path added to `config.toml` today has no project row yet, so the run that
    first indexes it must still archive it. Restricting the rule to rows this
    run created is what keeps both properties true at the same time.
    """
    cfg, store, projects = aliased_env
    write(projects, "n.jsonl", transcript_lines(cwd=NEW))
    reindex(store, cfg)
    assert store.project_by_path(GONE) is None

    write(projects, "g.jsonl", transcript_lines(sid=SID_B, cwd=GONE),
          dirname=GONE_DIR)
    reindex(store, cfg)
    assert store.project_by_path(GONE)["status"] == "archived"


# --- Phase 3: the launcher ---------------------------------------------------
#
# Correlating a launch back to its transcript, which the two launch modes do
# differently. A terminal launch pre-assigns the session UUID, so the join is a
# fact the indexer creates for free the moment it writes the session — these
# tests assert it rather than build it. `claude --bg` ignores `--session-id` and
# mints its own, so a background launch holds only the 8-hex handle it printed,
# and the indexer's backfill is what turns that into a session id.
#
# No test here spawns anything: a launch row is constructed directly.

DEMO = "/Users/you/dev/demo"  # `transcript_lines`' default cwd
# Shares SID's first eight hex characters, which is the whole hazard: the handle
# `--bg` prints is exactly `session_id[:8]`.
SID_TWIN = "22222222-9999-9999-9999-999999999999"


def make_launch(store, project_id, mode, lid="L1", session_id=None, short_id=None):
    """A launch row as `launcher.launch` would have left it, before any index.

    Terminal mode carries `session_id` from the start; background mode carries
    only `short_id`, which `set_launch_session` is what stamps.
    """
    store.create_launch(models.Launch(
        id=lid, project_id=project_id, mode=mode, prompt="do the next thing",
        session_id=session_id, model="opus", effort="high",
        launched_at=1_780_000_000, outcome="started",
    ))
    if short_id is not None:
        store.set_launch_session(lid, session_id, short_id)
    return lid


def render_detail_page(cfg, store, project_id, tab="launches"):
    """Render `project.html` through the real workspace model (`tab=launches`
    by default, since every caller only inspects the launches table).

    Rendered directly rather than through the route because the route's
    context is another task's to extend; the filters come from `api` so a
    filter this template names but the app does not register fails here.
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from bridge import api
    from bridge.workspace import build_workspace

    env = Environment(
        loader=FileSystemLoader(str(Path(api.__file__).parent / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    api.register_template_filters(env)

    model = build_workspace(store, cfg, project_id, tab)
    return env.get_template("project.html").render(
        model=model, active="projects", totals={"last_5h": 0},
    )


def launches_table(html):
    """Just the launches table, so a match cannot come from elsewhere."""
    assert '<table class="launches"' in html
    return html.split('<table class="launches"', 1)[1].split("</table>", 1)[0]


def test_a_terminal_launch_joins_to_its_transcript_by_its_pre_assigned_uuid(env):
    """The spec requirement, asserted end to end: because the UUID was assigned
    before the spawn, the join exists as soon as the session is indexed."""
    cfg, store, projects = env
    pid = store.upsert_project(DEMO, "demo")
    make_launch(store, pid, "terminal", session_id=SID)
    write(projects, "s.jsonl", transcript_lines())

    reindex(store, cfg)

    row = launch_by_session(store, SID)
    assert row is not None, "the pre-assigned UUID must find the indexed session"
    assert row["id"] == "L1"
    session = store.session_row(SID)
    assert session is not None
    assert session["project_id"] == row["project_id"]
    assert store.get_project(row["project_id"])["path"] == DEMO


def test_a_background_launch_resolves_its_short_id_to_a_full_session_id(env):
    cfg, store, projects = env
    pid = store.upsert_project(DEMO, "demo")
    make_launch(store, pid, "background", short_id=SID[:8])
    write(projects, "s.jsonl", transcript_lines())

    stats = reindex(store, cfg)

    assert stats.launches_linked == 1
    row = store.launches(pid)[0]
    assert row["session_id"] == SID
    assert row["short_id"] == SID[:8], "the handle is kept, not overwritten"
    assert launch_by_session(store, SID)["id"] == "L1"


def test_two_sessions_sharing_a_short_id_prefix_leave_the_launch_unlinked(env):
    """Ambiguity is left null rather than guessed: binding a launch to a session
    Bridge did not start is worse than showing it unlinked."""
    cfg, store, projects = env
    pid = store.upsert_project(DEMO, "demo")
    make_launch(store, pid, "background", short_id=SID[:8])
    write(projects, "a.jsonl", transcript_lines())
    write(projects, "b.jsonl", transcript_lines(sid=SID_TWIN))

    stats = reindex(store, cfg)

    assert len(store.sessions(pid)) == 2, "both candidates must be indexed"
    assert stats.launches_linked == 0
    assert store.launches(pid)[0]["session_id"] is None
    assert launch_by_session(store, SID) is None
    assert launch_by_session(store, SID_TWIN) is None


def test_a_launch_whose_session_never_appears_stays_started_and_unlinked(env):
    """A spawn that started nothing, or a session quit before it wrote a
    transcript. Not an error, and not retried into a wrong answer."""
    cfg, store, projects = env
    pid = store.upsert_project(DEMO, "demo")
    make_launch(store, pid, "background", short_id="abcdef01")
    write(projects, "s.jsonl", transcript_lines())

    reindex(store, cfg)
    reindex(store, cfg)  # a second pass must not invent a match either

    row = store.launches(pid)[0]
    assert row["session_id"] is None
    assert row["outcome"] == "started"


def test_a_launch_with_no_matching_session_renders_the_detail_page(env):
    cfg, store, projects = env
    pid = store.upsert_project(DEMO, "demo")
    make_launch(store, pid, "background", short_id="abcdef01")
    write(projects, "s.jsonl", transcript_lines())
    reindex(store, cfg)

    table = launches_table(render_detail_page(cfg, store, pid))

    assert "background" in table
    assert "no session yet" in table
    assert "abcdef01" in table
    assert "Did work" not in table, "an unlinked launch must not borrow a session"


def test_a_linked_launch_shows_its_session_on_the_detail_page(env):
    """The launch and the session read as one row once the join exists."""
    cfg, store, projects = env
    pid = store.upsert_project(DEMO, "demo")
    make_launch(store, pid, "background", short_id=SID[:8])
    write(projects, "s.jsonl", transcript_lines())
    reindex(store, cfg)

    table = launches_table(render_detail_page(cfg, store, pid))

    assert "Did work" in table, "the launched session's own title"
    assert "no session yet" not in table


def test_against_the_real_corpus_no_launch_joins_a_session_it_did_not_launch(
    tmp_path, monkeypatch
):
    """Real session ids, real project directories, launches seeded from them.

    Fixtures choose their own ids, so only real ids can say whether an 8-hex
    prefix is unique in practice. The prefix census below covers the whole
    corpus (filenames are session ids, so it needs no parsing); the index itself
    runs over a bounded symlinked subset, because the full corpus is gigabytes
    and a test that slow would stop being run.

    The real `config.toml` is read here, against the autouse guard, because
    aliasing is what makes this test hard: collapsing two paths into one project
    *merges* their session sets, and a larger candidate set is exactly what
    makes an 8-hex prefix collision possible. Running it un-aliased would ask an
    easier question than the live panel answers.
    """
    monkeypatch.delenv("BRIDGE_CONFIG", raising=False)
    real = Path.home() / ".claude" / "projects"
    files = transcript_files(real)
    if not files:
        pytest.skip("no real transcript corpus on this machine")

    by_dir: dict[str, list[Path]] = {}
    for f in files:
        by_dir.setdefault(f.parent.name, []).append(f)

    ambiguous = 0
    for dir_name, group in by_dir.items():
        seen: dict[str, int] = {}
        for f in group:
            seen[f.stem[:8]] = seen.get(f.stem[:8], 0) + 1
        for prefix, n in seen.items():
            if n > 1:
                ambiguous += 1
                print(f"\nreal-corpus prefix collision: {dir_name} {prefix} x{n}")
    print(f"\nreal-corpus session ids: {len(files)}, "
          f"colliding 8-hex prefixes: {ambiguous}")

    # Symlinks, never copies: the corpus is read-only and 3.5 GB of it.
    projects = tmp_path / "projects"
    for dir_name, group in sorted(by_dir.items()):
        small = sorted(
            (f for f in group if 0 < f.stat().st_size <= 1024 * 1024),
            key=lambda p: p.stat().st_size,
        )[:8]
        for f in small:
            (projects / dir_name).mkdir(parents=True, exist_ok=True)
            (projects / dir_name / f.name).symlink_to(f)

    cfg = load({
        "claude_projects_dir": projects,
        "db_path": tmp_path / "real.db",
        "spool_dir": tmp_path / "spool",
        "launches_dir": tmp_path / "launches",
        "discovery_paths": (tmp_path / "dev",),
    })
    store = Store(cfg.db_path)
    try:
        reindex(store, cfg)
        indexed = [
            (p["id"], s["id"])
            for p in store.projects(include_hidden=True)
            for s in store.sessions(p["id"], limit=10_000)
        ]
        assert len(indexed) > 10, "the subset must be big enough to be a test"

        # Half the sessions get a terminal launch (session id pre-assigned), the
        # other half a background launch (handle only, for the backfill to
        # resolve), plus one background launch whose handle matches nothing.
        expected: dict[str, str | None] = {}
        half = len(indexed) // 2
        for i, (pid, sid) in enumerate(indexed):
            lid = f"real-{i}"
            if i < half:
                make_launch(store, pid, "terminal", lid=lid, session_id=sid)
            else:
                make_launch(store, pid, "background", lid=lid, short_id=sid[:8])
            expected[lid] = sid

        orphan_pid = indexed[0][0]
        orphan_handle = next(
            h for h in (f"{n:08x}" for n in range(1 << 20))
            if not any(sid.startswith(h) for _, sid in indexed)
        )
        make_launch(store, orphan_pid, "background", lid="real-orphan",
                    short_id=orphan_handle)
        expected["real-orphan"] = None

        reindex(store, cfg)  # the backfill pass

        rows = {
            r["id"]: r
            for p in store.projects(include_hidden=True)
            for r in store.launches(p["id"], limit=10_000)
        }
        assert set(rows) == set(expected)
        for lid, want in expected.items():
            row = rows[lid]
            assert row["session_id"] == want, (
                f"{lid} joined to {row['session_id']!r}, not the session it launched"
            )
            if want is None:
                continue
            session = store.session_row(want)
            assert session is not None
            assert session["project_id"] == row["project_id"], (
                f"{lid} joined across projects"
            )
        print(f"real-corpus launches checked: {len(rows)} "
              f"({half} terminal, {len(indexed) - half} background, 1 unmatched)")
    finally:
        store.close()


def test_usage_dedup_state_survives_across_index_runs(env):
    """The dedup marker must round-trip through the sessions row.

    A live transcript is indexed repeatedly while it grows, so the boundary
    routinely lands between two entries of one response. If the marker is not
    persisted and rehydrated, that response is counted twice -- and only for
    running sessions, which are exactly the ones on a card.
    """
    cfg, store, projects = env
    entry = jline(type="assistant", sessionId=SID, isSidechain=False,
                  requestId="req_A", timestamp="2026-07-30T10:05:00.000Z",
                  cwd="/Users/you/dev/demo",
                  message={"role": "assistant", "model": "claude-opus-5",
                           "usage": {"input_tokens": 100, "output_tokens": 50}})
    p = write(projects, "s.jsonl", [
        jline(type="user", sessionId=SID, isSidechain=False,
              timestamp="2026-07-30T10:00:00.000Z", cwd="/Users/you/dev/demo",
              message={"role": "user", "content": "go"}),
        entry,
    ])
    reindex(store, cfg)
    pid = store.projects()[0]["id"]
    assert store.latest_session(pid)["tokens_in"] == 100
    assert store.latest_session(pid)["last_usage_request_id"] == "req_A"

    # The same response's next entry arrives after the offset.
    with p.open("a") as f:
        f.write(entry)
    reindex(store, cfg)

    row = store.latest_session(pid)
    assert (row["tokens_in"], row["tokens_out"]) == (100, 50)
