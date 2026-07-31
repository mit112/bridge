"""Read-only git probe. Never mutates a repository.

Verified against the real environment:
  * `rev-list --left-right --count @{u}...HEAD` exits 128 with
    "no upstream configured" when no upstream exists -> ahead/behind stay None.
  * `rev-parse --abbrev-ref HEAD` returns the literal "HEAD" when detached,
    which matches `gitBranch: "HEAD"` values seen in real transcripts.
  * ~43% of tracked project paths are not repos at all, so `not_a_repo` is a
    normal outcome rather than a failure.
"""

import subprocess
from pathlib import Path

from bridge.models import GitState

GIT = "/usr/bin/git"


def _git(path: Path, *args: str, timeout: float) -> tuple[int, str]:
    proc = subprocess.run(
        [GIT, *args], cwd=path, capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, proc.stdout.strip()


def probe(path: Path, timeout: float = 2.0) -> GitState:
    path = Path(path)
    if not path.is_dir():
        return GitState(status="unavailable")
    try:
        code, out = _git(path, "rev-parse", "--is-inside-work-tree", timeout=timeout)
        if code != 0 or out != "true":
            return GitState(status="not_a_repo")

        g = GitState(status="ok")
        _, g.branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD", timeout=timeout)

        _, porcelain = _git(path, "status", "--porcelain", timeout=timeout)
        entries = [l for l in porcelain.splitlines() if l.strip()]
        g.dirty_count = len(entries)
        g.oldest_uncommitted_at = _oldest_mtime(path, entries)

        code, counts = _git(
            path, "rev-list", "--left-right", "--count", "@{u}...HEAD", timeout=timeout
        )
        if code == 0 and "\t" in counts:
            behind, ahead = counts.split("\t")[:2]
            g.behind, g.ahead = int(behind), int(ahead)

        code, last = _git(path, "log", "-1", "--format=%s%x09%ct", timeout=timeout)
        if code == 0 and "\t" in last:
            summary, ct = last.rsplit("\t", 1)
            g.last_commit_summary = summary
            g.last_commit_at = int(ct)
        return g
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return GitState(status="unavailable")


def _oldest_mtime(root: Path, porcelain_lines: list[str]) -> int | None:
    """Oldest mtime among changed files: how long work has sat uncommitted."""
    oldest: int | None = None
    for line in porcelain_lines:
        rel = line[3:].strip()
        if " -> " in rel:  # rename
            rel = rel.split(" -> ", 1)[1]
        rel = rel.strip().strip('"')
        try:
            mt = int((root / rel).stat().st_mtime)
        except OSError:
            continue
        if oldest is None or mt < oldest:
            oldest = mt
    return oldest
