import subprocess

import pytest

from bridge.gitprobe import probe

GIT = "/usr/bin/git"


def run(cwd, *args):
    subprocess.run([GIT, *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    run(d, "init", "-q")
    run(d, "config", "user.name", "t")
    run(d, "config", "user.email", "t@t")
    (d / "a.txt").write_text("hello\n")
    run(d, "add", "a.txt")
    run(d, "commit", "-q", "-m", "first commit")
    return d


def test_clean_repo(repo):
    g = probe(repo)
    assert g.status == "ok"
    assert g.branch in ("main", "master")
    assert g.dirty_count == 0
    assert g.last_commit_summary == "first commit"
    assert isinstance(g.last_commit_at, int)
    assert g.oldest_uncommitted_at is None


def test_dirty_repo_counts_and_ages(repo):
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").write_text("new\n")
    g = probe(repo)
    assert g.status == "ok"
    assert g.dirty_count == 2
    assert isinstance(g.oldest_uncommitted_at, int)


def test_no_upstream_yields_none_not_crash(repo):
    """`git rev-list @{u}` exits 128 with 'no upstream configured'."""
    g = probe(repo)
    assert g.status == "ok"
    assert g.ahead is None
    assert g.behind is None


def test_detached_head_reports_literal_HEAD(repo):
    run(repo, "checkout", "-q", "--detach")
    g = probe(repo)
    assert g.status == "ok"
    assert g.branch == "HEAD"


def test_not_a_repo_is_a_first_class_state(tmp_path):
    """~43% of real project paths are not repos. This is not an error."""
    plain = tmp_path / "plain"
    plain.mkdir()
    g = probe(plain)
    assert g.status == "not_a_repo"
    assert g.dirty_count == 0
    assert g.branch is None


def test_missing_path_is_unavailable(tmp_path):
    assert probe(tmp_path / "nope").status == "unavailable"


def test_timeout_yields_unavailable(repo, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    monkeypatch.setattr(subprocess, "run", boom)
    assert probe(repo).status == "unavailable"
