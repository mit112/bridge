import os
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


def test_renamed_file_with_quoted_destination_is_counted(repo):
    """Porcelain emits `R  a.txt -> "b renamed.txt"` when the new name needs quotes.

    Stripping quotes before splitting the arrow leaves a stray leading quote,
    which silently drops the file from the age computation.
    """
    run(repo, "mv", "a.txt", "b renamed.txt")
    old = 1_600_000_000
    os.utime(repo / "b renamed.txt", (old, old))
    g = probe(repo)
    assert g.status == "ok"
    assert g.dirty_count == 1
    assert g.oldest_uncommitted_at == old  # not None: the file was found


def test_timeout_value_reaches_subprocess(repo, monkeypatch):
    """A regression dropping `timeout=` from the git call must fail here."""
    seen = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        seen.append(kwargs.get("timeout"))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    probe(repo, timeout=1.25)
    assert seen, "probe made no subprocess calls"
    assert all(t == 1.25 for t in seen), seen


def test_default_timeout_is_two_seconds(repo, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        seen.append(kwargs.get("timeout"))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    probe(repo)
    assert seen and all(t == 2.0 for t in seen), seen


def test_oldest_uncommitted_at_is_genuinely_the_oldest(repo):
    """Deliberately makes the OLDEST file sort SECOND alphabetically.

    That way a 'return newest' bug and a 'return first file seen' bug both fail.
    """
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").write_text("new\n")
    newer, older = 1_700_000_000, 1_600_000_000
    os.utime(repo / "a.txt", (newer, newer))   # sorts first, but is NEWER
    os.utime(repo / "b.txt", (older, older))   # sorts second, but is OLDER
    g = probe(repo)
    assert g.dirty_count == 2
    assert g.oldest_uncommitted_at == older


def test_ahead_behind_against_a_real_upstream(repo, tmp_path):
    """`rev-list --left-right --count @{u}...HEAD` yields behind<TAB>ahead.

    Swapping the two would pass a test that only checks 0/0, so this commits
    locally to force ahead=1, behind=0.
    """
    bare = tmp_path / "origin.git"
    subprocess.run([GIT, "init", "--bare", "-q", str(bare)],
                   check=True, capture_output=True, text=True)
    run(repo, "remote", "add", "origin", str(bare))
    branch = subprocess.run([GIT, "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()
    run(repo, "push", "-q", "-u", "origin", branch)

    synced = probe(repo)
    assert synced.status == "ok"
    assert (synced.ahead, synced.behind) == (0, 0)

    (repo / "c.txt").write_text("c\n")
    run(repo, "add", "c.txt")
    run(repo, "commit", "-q", "-m", "local only")

    ahead_one = probe(repo)
    assert ahead_one.ahead == 1, "ahead/behind may be swapped"
    assert ahead_one.behind == 0
