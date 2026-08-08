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
