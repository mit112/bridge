import pytest

from bridge.config import load
from bridge.indexer import reindex
from bridge.store import Store
from tests.conftest import jline

SID = "22222222-2222-2222-2222-222222222222"


def transcript_lines(sid=SID, cwd="/Users/mitsheth/dev/demo", title="Did work"):
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
    (projects / "-Users-mitsheth-dev-demo").mkdir(parents=True)
    cfg = load({"claude_projects_dir": projects, "db_path": tmp_path / "b.db"})
    store = Store(cfg.db_path)
    yield cfg, store, projects
    store.close()


def write(projects, name, lines, dirname="-Users-mitsheth-dev-demo"):
    p = projects / dirname / name
    p.write_text("".join(lines))
    return p


def test_first_index_creates_project_and_session(env):
    cfg, store, projects = env
    write(projects, "s.jsonl", transcript_lines())
    stats = reindex(store, cfg)
    assert stats.files_scanned == 1
    assert stats.sessions_upserted == 1
    projs = store.projects()
    assert len(projs) == 1
    assert projs[0]["path"] == "/Users/mitsheth/dev/demo"
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
    p.write_text("".join(transcript_lines(title="Rewritten")))
    stats = reindex(store, cfg)
    assert stats.files_scanned == 1
    pid = store.projects()[0]["id"]
    assert store.latest_session(pid)["title"] == "Rewritten"


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


def test_two_projects_are_separated(env):
    cfg, store, projects = env
    (projects / "-Users-mitsheth-dev-other").mkdir()
    write(projects, "a.jsonl", transcript_lines())
    write(projects, "b.jsonl",
          transcript_lines(sid="33333333-3333-3333-3333-333333333333",
                           cwd="/Users/mitsheth/dev/other"),
          dirname="-Users-mitsheth-dev-other")
    reindex(store, cfg)
    assert {p["name"] for p in store.projects()} == {"demo", "other"}
