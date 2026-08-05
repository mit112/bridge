import dataclasses

import pytest

from bridge import backfill
from bridge.config import load
from bridge.store import Store

STRUCTURED = """# Handoff

**Date:** 2026-07-30
Some status prose that is not the prompt.

## Next session

Continue the widget refactor in ~/dev/widget. The adapter is done and committed;
the renderer is not. Do not revert the naming decision.

## Ledger

Not part of the prompt.
"""

UNSTRUCTURED = """# Some notes

No recognizable prompt section anywhere in this file, just prose about what
happened and a few `commands` to run.
"""


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    cfg = load({
        "db_path": tmp_path / "bf.db",
        "spool_dir": tmp_path / "spool",
        "discovery_paths": (projects,),
    })
    store = Store(cfg.db_path)
    yield store, cfg, projects
    store.close()


def make_project(projects, name, filename, text):
    root = projects / name
    root.mkdir(parents=True, exist_ok=True)
    (root / filename).write_text(text)
    return root


def test_extract_takes_only_the_delimited_next_session_section():
    prompt, structured = backfill.extract_prompt(STRUCTURED)
    assert structured is True
    assert prompt.startswith("Continue the widget refactor")
    assert "Not part of the prompt" not in prompt
    assert "Some status prose" not in prompt


def test_a_file_with_no_recognizable_section_keeps_the_whole_file():
    prompt, structured = backfill.extract_prompt(UNSTRUCTURED)
    assert structured is False
    assert "No recognizable prompt section" in prompt
    assert prompt.startswith("# Some notes")


def test_dry_run_is_the_default_and_writes_nothing(env):
    store, cfg, projects = env
    make_project(projects, "widget", "HANDOFF.md", STRUCTURED)

    stats = backfill.run(store, cfg)

    assert stats.dry_run is True
    assert stats.found == 1
    assert stats.written == 0
    assert store.handoff_count() == 0


def test_writing_twice_creates_one_handoff_per_file(env):
    """The id is derived from path and contents, so a re-run is a no-op."""
    store, cfg, projects = env
    make_project(projects, "widget", "HANDOFF.md", STRUCTURED)
    make_project(projects, "gadget", "NEXT-SESSION.md", UNSTRUCTURED)

    first = backfill.run(store, cfg, write=True)
    second = backfill.run(store, cfg, write=True)

    assert first.found == 2
    assert first.written == 2
    assert second.found == 2
    assert second.written == 0, "a second --write must import nothing new"
    assert store.handoff_count() == 2


def test_an_unstructured_file_is_flagged_in_its_summary(env):
    store, cfg, projects = env
    root = make_project(projects, "gadget", "HANDOFF.md", UNSTRUCTURED)

    stats = backfill.run(store, cfg, write=True)

    assert stats.unstructured == 1
    pid = store.project_by_path(str(root))["id"]
    row = store.queued_handoff(pid)
    assert "unstructured" in row["summary"]
    assert "No recognizable prompt section" in row["next_prompt"]


def test_an_empty_handoff_file_is_skipped(env):
    store, cfg, projects = env
    make_project(projects, "blank", "HANDOFF.md", "\n\n   \n")
    assert backfill.run(store, cfg, write=True).found == 0


def test_editing_the_file_produces_a_new_handoff_that_supersedes(env):
    """A rewritten handoff file is new content and should queue afresh."""
    store, cfg, projects = env
    root = make_project(projects, "widget", "HANDOFF.md", STRUCTURED)
    backfill.run(store, cfg, write=True)

    (root / "HANDOFF.md").write_text(
        STRUCTURED.replace("Continue the widget refactor", "Actually do something else")
    )
    second = backfill.run(store, cfg, write=True)

    assert second.written == 1
    pid = store.project_by_path(str(root))["id"]
    assert store.queued_handoff(pid)["next_prompt"].startswith("Actually do something")
    assert len(store.handoffs(pid)) == 2


def test_discovery_repos_lists_only_git_repos_under_discovery_paths(env, tmp_path):
    store, cfg, projects = env
    dev = tmp_path / "dev"
    (dev / "has-git" / ".git").mkdir(parents=True)
    (dev / "plain-dir").mkdir()
    (dev / "a-file.txt").parent.mkdir(exist_ok=True)
    (dev / "a-file.txt").write_text("x")
    cfg = dataclasses.replace(cfg, discovery_paths=(dev,))
    assert backfill.discovery_repos(cfg) == [dev / "has-git"]


def test_against_the_real_files_on_this_machine(tmp_path):
    """Runs discovery over the real ~/dev, read-only, and records what it finds.

    Fixtures cannot represent how inconsistent these files actually are; three
    Phase 1 bugs were reachable only against real input.
    """
    cfg = load({"db_path": tmp_path / "real.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    try:
        found = backfill.discover(store, cfg)
        stats = backfill.run(store, cfg)  # dry run: touches nothing
    finally:
        store.close()

    assert stats.written == 0, "the default run must never write"
    assert stats.dry_run is True
    for candidate in found:
        assert candidate.prompt.strip(), f"{candidate.path} produced an empty prompt"
        assert candidate.id, "every candidate needs a deterministic id"
        # Read-only: the file is still there, unmodified.
        assert candidate.path.is_file()
    print(f"\nreal-corpus backfill: {len(found)} file(s)")
    for c in found:
        kind = "structured" if c.structured else "UNSTRUCTURED"
        print(f"  {kind:12} {len(c.prompt):6d} chars  {c.path}")
