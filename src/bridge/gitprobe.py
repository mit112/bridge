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
    """Returns (returncode, RAW stdout).

    Deliberately unstripped: `git status --porcelain` encodes status in the
    first two columns, and an unstaged modification is ' M path'. Stripping
    the whole output shifts that line left and corrupts the path. The
    NUL-separated `-z` form is byte-exact for the same reason and must not be
    touched either.

    `surrogateescape` because `-z` emits real filesystem bytes rather than
    git's ASCII-safe quoted form: a path that is not valid UTF-8 would
    otherwise raise `UnicodeDecodeError` here and cost the whole probe. The
    surrogates round-trip back through `stat()` unchanged.
    """
    proc = subprocess.run(
        [GIT, *args], cwd=path, capture_output=True, text=True,
        errors="surrogateescape", timeout=timeout,
    )
    return proc.returncode, proc.stdout


def probe(path: Path, timeout: float = 2.0) -> GitState:
    path = Path(path)
    if not path.is_dir():
        return GitState(status="unavailable")
    try:
        code, out = _git(path, "rev-parse", "--is-inside-work-tree", timeout=timeout)
        if code != 0 or out.strip() != "true":
            return GitState(status="not_a_repo")

        g = GitState(status="ok")
        _, branch_out = _git(path, "rev-parse", "--abbrev-ref", "HEAD", timeout=timeout)
        g.branch = branch_out.strip()

        # A repo with no commits yet has no HEAD to resolve, and `rev-parse`
        # exits non-zero there. That is a normal state, not a probe failure, so
        # it leaves `head` None rather than failing the whole probe.
        code, head_out = _git(path, "rev-parse", "HEAD", timeout=timeout)
        g.head = head_out.strip() if code == 0 else None

        _, porcelain = _git(path, "status", "--porcelain", "-z", timeout=timeout)
        entries = _porcelain_paths(porcelain)
        g.dirty_count = len(entries)
        g.oldest_uncommitted_at = _oldest_mtime(path, entries)

        code, counts = _git(
            path, "rev-list", "--left-right", "--count", "@{u}...HEAD", timeout=timeout
        )
        counts = counts.strip()
        if code == 0 and "\t" in counts:
            behind, ahead = counts.split("\t")[:2]
            g.behind, g.ahead = int(behind), int(ahead)

        code, last = _git(path, "log", "-1", "--format=%s%x09%ct", timeout=timeout)
        last = last.strip()
        if code == 0 and "\t" in last:
            summary, ct = last.rsplit("\t", 1)
            g.last_commit_summary = summary
            g.last_commit_at = int(ct)
        return g
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return GitState(status="unavailable")


def _porcelain_paths(raw: str) -> list[str]:
    """One path per changed entry, from `git status --porcelain -z`.

    `-z` is the only form that round-trips a path faithfully. In the default
    text form git applies `core.quotepath`, so `café.txt` comes back as the
    literal `"caf\\303\\251.txt"`: stripping the quotes leaves the octal
    escapes, `stat()` fails, and the file silently drops out of the
    uncommitted-age computation while still counting toward `dirty_count`.
    `-z` emits raw bytes, NUL-separated and never quoted, so a non-ASCII path,
    a path containing a space and a path containing a newline all survive.

    A rename or copy is TWO consecutive records -- the new path first, then the
    origin -- with no ` -> ` arrow to split on. The origin record is consumed
    here so it neither inflates the count nor gets `stat()`ed at a path that no
    longer exists.
    """
    records = [r for r in raw.split("\0") if r]
    paths: list[str] = []
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if len(rec) < 4:
            continue  # not an `XY <path>` record; nothing addressable
        if rec[0] in ("R", "C") or rec[1] in ("R", "C"):
            i += 1  # skip the origin-path record that follows
        paths.append(rec[3:])
    return paths


def _oldest_mtime(root: Path, rel_paths: list[str]) -> int | None:
    """Oldest mtime among changed files: how long work has sat uncommitted."""
    oldest: int | None = None
    for rel in rel_paths:
        try:
            mt = int((root / rel).stat().st_mtime)
        except OSError:
            continue
        if oldest is None or mt < oldest:
            oldest = mt
    return oldest
