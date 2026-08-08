"""The one update primitive the CLI (`bridge update`) and the panel button
(`POST /api/update`) both call.

It never installs the floating `@main`: the check resolves a concrete SHA and
the install pins that exact SHA. The running commit is read from the installer's
PEP 610 `direct_url.json` (git installs), falling back to the `_build` sentinel
that the Homebrew formula stamps."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bridge import _build

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/mit112/bridge.git"
REPO_REF = "refs/heads/main"

InstallMethod = Literal["uv", "brew", "dev", "unknown"]
Classification = Literal["current", "behind", "diverged", "unknown"]

_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class UpdateState:
    state: Literal["current", "behind", "diverged", "unknown", "stale"]
    installed_sha: str | None
    latest_sha: str | None
    checked_at: str | None
    error: str | None


@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    previous_sha: str | None
    attempted_sha: str
    method: InstallMethod
    started_at: str
    ended_at: str | None
    exit_status: int | None
    log_path: str
    error: str | None
    rolled_back: bool


def _read_direct_url() -> str | None:
    """The raw text of this distribution's PEP 610 direct_url.json, or None."""
    try:
        from importlib.metadata import distribution
        return distribution("bridge").read_text("direct_url.json")
    except Exception:
        return None


def installed_sha() -> str | None:
    """The exact commit this install was built from, or None for dev/editable.

    1. A git install (`uv tool install git+...@<sha>`) records the resolved
       commit in direct_url.json's `vcs_info.commit_id` (PEP 610) -- full 40-hex.
    2. Otherwise fall back to the `_build.COMMIT_SHA` sentinel, which the Homebrew
       formula stamps at install time.
    3. Editable/dev installs (dir_info.editable, or an unstamped sentinel) have no
       verifiable commit -> None, so the caller never nudges."""
    raw = _read_direct_url()
    if raw:
        try:
            commit = json.loads(raw).get("vcs_info", {}).get("commit_id", "")
        except (ValueError, AttributeError):
            commit = ""
        if isinstance(commit, str) and _SHA_RE.match(commit):
            return commit
    sha = getattr(_build, "COMMIT_SHA", "unknown")
    if isinstance(sha, str) and _SHA_RE.match(sha):
        return sha
    return None


def _running_executable() -> Path:
    """The resolved path of the console script that started this process."""
    return Path(sys.argv[0]).resolve()


def _uv_tools_dir() -> Path:
    """uv's tools directory: `uv tool dir`, falling back to the default."""
    uv = shutil.which("uv")
    if uv is not None:
        try:
            proc = subprocess.run([uv, "tool", "dir"], capture_output=True,
                                  text=True, check=False, timeout=5)
            if proc.returncode == 0 and proc.stdout.strip():
                return Path(proc.stdout.strip()).resolve()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return (Path.home() / ".local" / "share" / "uv" / "tools").resolve()


def _brew_cellars() -> list[Path]:
    """Both Homebrew prefixes' Cellars: Apple silicon and Intel."""
    out = []
    for prefix in ("/opt/homebrew", "/usr/local"):
        cellar = Path(prefix) / "Cellar"
        if cellar.is_dir():
            out.append(cellar.resolve())
    return out


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def install_method() -> InstallMethod:
    """Resolve the running executable against package-manager prefixes.

    Never guesses: an executable under neither a uv-tools dir nor a Homebrew
    Cellar is "dev" when the build SHA is a dev sentinel (editable/source), and
    "unknown" otherwise (pipx/ambiguous) -- so an ambiguous install is refused
    rather than updated as if it were uv."""
    exe = _running_executable()
    if _is_within(exe, _uv_tools_dir()):
        return "uv"
    for cellar in _brew_cellars():
        if _is_within(exe, cellar):
            return "brew"
    return "dev" if installed_sha() is None else "unknown"


def resolve_remote_sha(url: str = REPO_URL, ref: str = REPO_REF,
                       timeout: float = 8.0) -> str | None:
    """The remote SHA for `ref` via `git ls-remote` -- no API rate limit.

    Returns None on any failure (timeout, network error, unexpected output):
    the caller keeps its last known result as stale and never infers an update."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        proc = subprocess.run(
            [git, "ls-remote", url, ref],
            capture_output=True, text=True, check=False, timeout=timeout,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("git ls-remote failed: %s", exc)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        log.warning("git ls-remote returned %d: %s", proc.returncode,
                    proc.stderr.strip())
        return None
    sha = proc.stdout.split()[0].strip()
    return sha if len(sha) == 40 else None


def _update_cache_repo() -> Path:
    """A bare object cache used only to answer ancestry questions offline."""
    return Path.home() / ".bridge" / "update" / "repo.git"


def _is_ancestor(installed: str, remote: str) -> bool | None:
    """True if `installed` is an ancestor of `remote` (remote is a fast-forward
    descendant). None when it cannot be decided (objects absent / git error) --
    which the classifier treats as fail-closed "unknown"."""
    git = shutil.which("git")
    repo = _update_cache_repo()
    if git is None or not repo.is_dir():
        return None
    try:
        proc = subprocess.run(
            [git, "-C", str(repo), "merge-base", "--is-ancestor", installed, remote],
            capture_output=True, text=True, check=False, timeout=8,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None  # 128 == a SHA the cache does not have; indeterminate


def classify(installed: str | None, remote: str | None, *,
             is_ancestor=_is_ancestor) -> Classification:
    """current / behind / diverged / unknown. Nudge only on `behind`.

    `behind` requires the remote to be a fast-forward descendant of the
    installed SHA; anything unknowable is `unknown`, never `behind`."""
    if installed is None or remote is None:
        return "unknown"
    if installed == remote:
        return "current"
    verdict = is_ancestor(installed, remote)
    if verdict is True:
        return "behind"
    if verdict is False:
        return "diverged"
    return "unknown"
