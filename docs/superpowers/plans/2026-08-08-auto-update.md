# Bridge Auto-Update (Sections B + C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Bridge one shared `bridge update` engine plus a background update-check and a secured one-click panel button, so friends tracking `main` HEAD can stay current safely.

**Architecture:** Bridge learns its own commit from the PEP 610 `direct_url.json` the installer records for git installs (`vcs_info.commit_id`), falling back to a plain committed `src/bridge/_build.py` sentinel that the Homebrew formula stamps at install time — no build hook. A new `bridge.update` module is the single primitive both the CLI (`bridge update`) and the panel button (`POST /api/update`) call: it detects the install method by resolving the running executable against package-manager prefixes, resolves the remote SHA with `git ls-remote`, classifies current/behind/diverged/unknown (fail-closed), and runs the install as a lockfile-guarded transaction that verifies a fresh PID reports the target SHA and rolls back on mismatch. A bounded worker thread — isolated from the 15s refresh loop — polls the remote SHA on a ~30-minute cache and surfaces an `update` object on the status API the panel already polls (`/api/diagnostics`). The panel shows a per-SHA-dismissible banner with a copy-able `bridge update` fallback and a secured one-click button.

**Tech Stack:** Python 3.13, FastAPI + uvicorn + jinja2, hatchling build (no custom hook), uv-managed, pytest + `fastapi.testclient.TestClient`, macOS-only. stdlib `subprocess`, `urllib`, `threading`, `secrets`, `tomllib`, `importlib.metadata`, `json`.

## Global Constraints

- **macOS-only**, Python **>= 3.13**. (`pyproject.toml`)
- **No new runtime dependencies.** Bridge ships `fastapi`, `uvicorn[standard]`, `jinja2` only; the update engine uses stdlib + the `git` and `uv`/`brew` binaries already on the machine. (Matches the CLI's stdlib-`urllib` discipline — `cli.py:26-29`.)
- **Never install the floating `@main`.** The check resolves a SHA; the update installs **that exact SHA**; post-install verifies the running process reports it. (Spec invariant 1.)
- **Fail closed.** On timeout / network error / non-fast-forward / indeterminate ancestry, keep the last known result as `stale` and **never** infer "update available". A nudge appears **only** on state `behind` (remote is a fast-forward descendant of installed). (Spec invariant 2, §C.)
- **Editable/dev installs legitimately report `unknown`** and never nudge. A **distributable** build that cannot determine its SHA **fails the build** rather than shipping "unknown". (Spec §C.)
- **`/api/update` is CSRF-guarded**, not loopback-`Host`-guarded alone: per-install bearer token from a `0600` file **and** `Origin` + `Sec-Fetch-Site` validation **and** a confirmation showing current→new SHA. (Spec invariant 3.)
- **Every update is a transaction:** lockfile rejects concurrent updates; previous/attempted SHA, method, times, exit, log path recorded; verify fresh-PID target SHA; roll back on mismatch (uv) or print exact recovery (brew). (Spec invariant 4, §B.)
- **uv path:** `uv tool install --force --reinstall git+https://github.com/mit112/bridge@<sha>`, non-interactive. **brew path:** `brew upgrade --fetch-HEAD mit112/bridge/bridge` with `HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1`. (Spec §B.)
- **Update check runs on a SEPARATE bounded worker**, isolated from the ~15s refresh loop (`__main__.py:158`, `:211`) so a network hang can't stall indexing/shutdown. (Spec §C.)
- **Privacy:** README/setup document that Bridge contacts GitHub to check for updates; a persistent `[update] enabled = false` in `config.toml` turns it off; **no GitHub token** is collected or stored. (Spec §C.)
- **One-click self-restart uses a SEPARATE one-shot LaunchAgent job** (not a child of the Bridge job — else `kickstart -k` kills it), re-bootstraps the plist from the new install path (`setup.py:292`, `:330`), restarts, and writes an update-state file the panel reads on SSE reconnect. (Spec §C.)
- **Style:** value types (frozen dataclasses), `Literal` for closed sets, `OSLog`-equivalent `logging.getLogger(__name__)` over `print` in library code, surgical diffs. Follow existing `api.py` middleware/route patterns and `refresh.py` worker patterns.

## Shared Interface Contract (authoritative names — every task must match these exactly)

```python
# src/bridge/update.py
REPO_URL = "https://github.com/mit112/bridge.git"
REPO_REF = "refs/heads/main"

InstallMethod = Literal["uv", "brew", "dev", "unknown"]
Classification = Literal["current", "behind", "diverged", "unknown"]

@dataclass(frozen=True)
class UpdateState:
    state: Literal["current", "behind", "diverged", "unknown", "stale"]
    installed_sha: str | None
    latest_sha: str | None
    checked_at: str | None            # ISO-8601 UTC, e.g. "2026-08-08T12:00:00+00:00"
    error: str | None

@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    previous_sha: str | None
    attempted_sha: str
    method: InstallMethod
    started_at: str                   # ISO-8601 UTC
    ended_at: str | None              # ISO-8601 UTC or None if never finished
    exit_status: int | None           # installer process exit code
    log_path: str                     # absolute path to the install log
    error: str | None
    rolled_back: bool

def installed_sha() -> str | None: ...                       # PEP 610 direct_url.json vcs commit; else _build sentinel; None for dev
def install_method() -> InstallMethod: ...
def resolve_remote_sha(url: str = REPO_URL, ref: str = REPO_REF,
                       timeout: float = 8.0) -> str | None: ...  # None on failure (fail closed)
def classify(installed: str | None, remote: str | None) -> Classification: ...
def run_update(target_sha: str) -> UpdateResult: ...
```

The status API `update` object (on `GET /api/diagnostics`) is exactly `asdict(UpdateState)`:
`{"state", "installed_sha", "latest_sha", "checked_at", "error"}`.

---

### Task 1: `installed_sha()` via PEP 610 `direct_url.json` (+ `update.py` skeleton + `_build.py` sentinel)

**Files:**
- Create: `src/bridge/update.py` (module skeleton: constants, dataclasses, `installed_sha()`)
- Create: `src/bridge/_build.py` (plain committed sentinel `COMMIT_SHA = "unknown"`; the Homebrew formula stamps the real SHA at install — see Plan 3. NO build hook, NO pyproject change.)
- Test: `tests/test_update_installed_sha.py`

**Interfaces:**
- Produces: `src/bridge/update.py` exposing `REPO_URL`, `REPO_REF`, the `InstallMethod`/`Classification` Literals, the frozen `UpdateState` and `UpdateResult` dataclasses (verbatim from the Shared Interface Contract), and `installed_sha() -> str | None`. `installed_sha()` returns the full 40-hex commit from the installer's `direct_url.json` (`vcs_info.commit_id`) for a git install; else the `_build.COMMIT_SHA` sentinel when a formula has stamped a real SHA; else `None` (editable/dev/unknown → never nudge).
- Consumes: nothing (first task).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_update_installed_sha.py
import json
import bridge.update as U


def test_git_install_returns_vcs_commit(monkeypatch):
    payload = json.dumps({"url": U.REPO_URL,
                          "vcs_info": {"vcs": "git", "commit_id": "a" * 40}})
    monkeypatch.setattr(U, "_read_direct_url", lambda: payload)
    assert U.installed_sha() == "a" * 40


def test_editable_install_returns_none(monkeypatch):
    payload = json.dumps({"url": "file:///repo", "dir_info": {"editable": True}})
    monkeypatch.setattr(U, "_read_direct_url", lambda: payload)
    monkeypatch.setattr(U._build, "COMMIT_SHA", "unknown")
    assert U.installed_sha() is None


def test_falls_back_to_build_sentinel(monkeypatch):
    monkeypatch.setattr(U, "_read_direct_url", lambda: None)  # no direct_url (e.g. Homebrew)
    monkeypatch.setattr(U._build, "COMMIT_SHA", "b" * 40)
    assert U.installed_sha() == "b" * 40


def test_unknown_when_no_source(monkeypatch):
    monkeypatch.setattr(U, "_read_direct_url", lambda: None)
    monkeypatch.setattr(U._build, "COMMIT_SHA", "unknown")
    assert U.installed_sha() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_update_installed_sha.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bridge.update'`.

- [ ] **Step 3: Write the `_build.py` sentinel**

```python
# src/bridge/_build.py
"""Commit-SHA sentinel. A plain committed module — NOT written by any build
hook. Editable/dev installs keep "unknown". The Homebrew formula overwrites
COMMIT_SHA with the resolved commit at install time (see the Homebrew plan);
git installs via uv don't need it because installed_sha() reads the commit
from the installer's PEP 610 direct_url.json instead."""

COMMIT_SHA = "unknown"
```

- [ ] **Step 4: Write the `update.py` skeleton + `installed_sha()`**

```python
# src/bridge/update.py
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
        if _SHA_RE.match(commit or ""):
            return commit
    sha = getattr(_build, "COMMIT_SHA", "unknown")
    if _SHA_RE.match(sha or ""):
        return sha
    return None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_update_installed_sha.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Confirm no build-system change and a clean tree**

Run: `git status --porcelain pyproject.toml` (expected: empty — Task 1 does NOT touch pyproject) and `uv build 2>&1 | tail -2` (expected: builds an sdist AND a wheel with no error, because there is no build hook to fail).

- [ ] **Step 7: Commit**

```bash
git add src/bridge/update.py src/bridge/_build.py tests/test_update_installed_sha.py
git commit -m "feat: installed_sha() via PEP 610 direct_url.json + _build sentinel"
```

---

### Task 2: `install_method()`

**Files:**
- Modify: `src/bridge/update.py`
- Test: `tests/test_update_method.py`

**Interfaces:**
- Consumes: the `update.py` skeleton + `installed_sha()` (Task 1).
- Produces:
  - `install_method() -> InstallMethod` where `InstallMethod = Literal["uv", "brew", "dev", "unknown"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_update_method.py
import bridge.update as U


def test_install_method_uv(monkeypatch, tmp_path):
    exe = tmp_path / "uv" / "tools" / "bridge" / "bin" / "bridge"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(U, "_running_executable", lambda: exe)
    monkeypatch.setattr(U, "_uv_tools_dir", lambda: tmp_path / "uv" / "tools")
    monkeypatch.setattr(U, "_brew_cellars", lambda: [])
    assert U.install_method() == "uv"


def test_install_method_brew_opt_homebrew(monkeypatch, tmp_path):
    cellar = tmp_path / "opt" / "homebrew" / "Cellar"
    exe = cellar / "bridge" / "0.1.0" / "bin" / "bridge"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(U, "_running_executable", lambda: exe)
    monkeypatch.setattr(U, "_uv_tools_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(U, "_brew_cellars", lambda: [cellar])
    assert U.install_method() == "brew"


def test_install_method_unknown_when_ambiguous(monkeypatch, tmp_path):
    exe = tmp_path / "somewhere" / "bin" / "bridge"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(U, "_running_executable", lambda: exe)
    monkeypatch.setattr(U, "_uv_tools_dir", lambda: tmp_path / "uv")
    monkeypatch.setattr(U, "_brew_cellars", lambda: [tmp_path / "cellar"])
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    assert U.install_method() == "unknown"


def test_install_method_dev_when_editable(monkeypatch, tmp_path):
    exe = tmp_path / "somewhere" / "bin" / "bridge"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(U, "_running_executable", lambda: exe)
    monkeypatch.setattr(U, "_uv_tools_dir", lambda: tmp_path / "uv")
    monkeypatch.setattr(U, "_brew_cellars", lambda: [tmp_path / "cellar"])
    monkeypatch.setattr(U, "installed_sha", lambda: None)
    assert U.install_method() == "dev"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_update_method.py -v`
Expected: FAIL — `AttributeError: module 'bridge.update' has no attribute 'install_method'` (the module imports fine; only `install_method` is missing).

- [ ] **Step 3: Write the minimal implementation**

`update.py` already exists from Task 1 (module header, imports, constants, dataclasses, `_read_direct_url()`, and `installed_sha()`). Append only the new function and its helpers:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_update_method.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Commit**

```bash
git add src/bridge/update.py tests/test_update_method.py
git commit -m "feat: detect install method"
```

---

### Task 3: `resolve_remote_sha()` + `classify()`

**Files:**
- Modify: `src/bridge/update.py`
- Test: `tests/test_update_classify.py`

**Interfaces:**
- Consumes: `REPO_URL`, `REPO_REF` (Task 2).
- Produces:
  - `resolve_remote_sha(url: str = REPO_URL, ref: str = REPO_REF, timeout: float = 8.0) -> str | None` — parses `git ls-remote`; `None` on any failure (fail closed).
  - `classify(installed: str | None, remote: str | None) -> Classification`. Ancestry is checked via keyword-only `is_ancestor` (default `_is_ancestor`), keeping the contract's positional `classify(installed, remote)` valid. **Note (adaptation):** the contract lists `classify(installed, remote)`; the keyword-only `is_ancestor` has a default so every call site uses the two-arg form — recorded per the plan's "adapt but note" rule.
  - `_is_ancestor(installed: str, remote: str) -> bool | None` — `True`/`False`/`None` (indeterminate) via a local object cache; monkeypatched in tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_update_classify.py
import bridge.update as U


def test_resolve_remote_sha_parses_ls_remote(monkeypatch):
    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stdout = "deadbeef" * 5 + "\trefs/heads/main\n"
            stderr = ""
        return P()
    monkeypatch.setattr(U.subprocess, "run", fake_run)
    assert U.resolve_remote_sha() == "deadbeef" * 5


def test_resolve_remote_sha_none_on_failure(monkeypatch):
    def fake_run(cmd, **kw):
        class P:
            returncode = 128
            stdout = ""
            stderr = "could not read from remote"
        return P()
    monkeypatch.setattr(U.subprocess, "run", fake_run)
    assert U.resolve_remote_sha() is None


def test_resolve_remote_sha_none_on_timeout(monkeypatch):
    def boom(cmd, **kw):
        raise U.subprocess.TimeoutExpired(cmd, 8.0)
    monkeypatch.setattr(U.subprocess, "run", boom)
    assert U.resolve_remote_sha() is None


def test_classify_current_when_equal():
    assert U.classify("a" * 40, "a" * 40) == "current"


def test_classify_unknown_when_missing():
    assert U.classify(None, "a" * 40) == "unknown"
    assert U.classify("a" * 40, None) == "unknown"


def test_classify_behind_when_remote_is_descendant():
    assert U.classify("a" * 40, "b" * 40, is_ancestor=lambda i, r: True) == "behind"


def test_classify_diverged_when_not_descendant():
    assert U.classify("a" * 40, "b" * 40, is_ancestor=lambda i, r: False) == "diverged"


def test_classify_unknown_when_ancestry_indeterminate():
    # Fail closed: an unknowable ancestry never infers an available update.
    assert U.classify("a" * 40, "b" * 40, is_ancestor=lambda i, r: None) == "unknown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_update_classify.py -v`
Expected: FAIL — `AttributeError: module 'bridge.update' has no attribute 'resolve_remote_sha'`.

- [ ] **Step 3: Write the minimal implementation (append to `update.py`)**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_update_classify.py -v`
Expected: PASS (all eight).

- [ ] **Step 5: Commit**

```bash
git add src/bridge/update.py tests/test_update_classify.py
git commit -m "feat: resolve remote SHA and classify update state (fail closed)"
```

---

### Task 4: Update-transaction lockfile + persistent state file

**Files:**
- Modify: `src/bridge/update.py`
- Test: `tests/test_update_transaction.py`

**Interfaces:**
- Consumes: `UpdateState`, `UpdateResult` (already defined in `update.py` by Task 1 — do NOT redefine).
- Produces:
  - `_lock_path() -> Path`, `_acquire_lock() -> int | None` (fd, or None if held), `_release_lock(fd: int) -> None`.
  - `write_update_state(state: UpdateState) -> Path`, `read_update_state() -> UpdateState | None` (persist to `~/.bridge/update/state.json`).
  - `_now_iso() -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_update_transaction.py
import bridge.update as U


def test_lock_rejects_concurrent(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    fd = U._acquire_lock()
    assert fd is not None
    assert U._acquire_lock() is None    # second acquire is refused
    U._release_lock(fd)
    fd2 = U._acquire_lock()             # released -> acquirable again
    assert fd2 is not None
    U._release_lock(fd2)


def test_state_roundtrips(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    st = U.UpdateState(state="behind", installed_sha="a" * 40,
                       latest_sha="b" * 40, checked_at="2026-08-08T00:00:00+00:00",
                       error=None)
    U.write_update_state(st)
    assert U.read_update_state() == st


def test_read_state_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    assert U.read_update_state() is None


def test_now_iso_is_utc():
    assert U._now_iso().endswith("+00:00")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_update_transaction.py -v`
Expected: FAIL — `AttributeError: ... '_acquire_lock'`.

- [ ] **Step 3: Write the minimal implementation (append to `update.py`. `UpdateState`/`UpdateResult` already exist from Task 1 — do NOT redefine. `json`/`os`/`Path`/`Literal` are already imported; add only `import dataclasses`, `import fcntl`, `from datetime import datetime, timezone`.)**

```python
# UpdateState / UpdateResult already exist from Task 1 -- do NOT redefine them.
# json, os, Path, Literal are already imported at the top of update.py (Task 1).
import dataclasses  # for dataclasses.asdict
import fcntl
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_dir() -> Path:
    d = Path.home() / ".bridge" / "update"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock_path() -> Path:
    return _update_dir() / "update.lock"


def _acquire_lock() -> int | None:
    """A non-blocking exclusive flock. Returns the fd on success, None if held.

    A concurrent update is refused rather than queued: two installers racing
    the same executable is exactly the corruption the transaction prevents."""
    fd = os.open(str(_lock_path()), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _state_path() -> Path:
    return _update_dir() / "state.json"


def write_update_state(state: UpdateState) -> Path:
    path = _state_path()
    path.write_text(json.dumps(dataclasses.asdict(state)), encoding="utf-8")
    return path


def read_update_state() -> UpdateState | None:
    path = _state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return UpdateState(**data)
    except (OSError, ValueError, TypeError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_update_transaction.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Commit**

```bash
git add src/bridge/update.py tests/test_update_transaction.py
git commit -m "feat: update-transaction dataclasses, lockfile, persistent state"
```

---

### Task 5: `run_update()` — install, verify fresh PID, roll back

**Files:**
- Modify: `src/bridge/update.py`
- Test: `tests/test_run_update.py`

**Interfaces:**
- Consumes: `install_method`, `installed_sha`, `_acquire_lock`/`_release_lock`, `write_update_state`, `_now_iso`, `UpdateResult` (Tasks 2/4).
- Produces:
  - `run_update(target_sha: str) -> UpdateResult`.
  - `_install_cmd(method: InstallMethod, sha: str) -> list[str]` and `_install_env(method) -> dict[str,str]`.
  - `_verify_fresh_pid(expected_sha: str) -> bool` — spawns a fresh `bridge --version` and asserts it reports `expected_sha` (short or full).
  - injectable seams `_run_installer(cmd, env, log_path) -> int` for tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_update.py
import bridge.update as U


def _stub(monkeypatch, tmp_path, method="uv", verify=True, installer_exit=0):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "install_method", lambda: method)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    calls = []
    def fake_installer(cmd, env, log_path):
        calls.append(cmd)
        return installer_exit
    monkeypatch.setattr(U, "_run_installer", fake_installer)
    monkeypatch.setattr(U, "_verify_fresh_pid", lambda sha: verify)
    return calls


def test_run_update_uv_success(monkeypatch, tmp_path):
    calls = _stub(monkeypatch, tmp_path)
    res = U.run_update("b" * 40)
    assert res.ok is True
    assert res.rolled_back is False
    assert res.attempted_sha == "b" * 40
    assert res.previous_sha == "a" * 40
    assert res.method == "uv"
    assert calls[0][:3] == ["uv", "tool", "install"]
    assert f"git+{U.REPO_URL[:-4]}@{'b' * 40}" in " ".join(calls[0])


def test_run_update_rolls_back_on_mismatch(monkeypatch, tmp_path):
    calls = _stub(monkeypatch, tmp_path, verify=False)
    res = U.run_update("b" * 40)
    assert res.ok is False
    assert res.rolled_back is True          # uv rollback reinstalls previous SHA
    assert calls[-1][:3] == ["uv", "tool", "install"]
    assert "a" * 40 in " ".join(calls[-1])  # reinstalled the previous SHA


def test_run_update_brew_prints_recovery_no_rollback(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, method="brew", verify=False)
    res = U.run_update("b" * 40)
    assert res.ok is False
    assert res.rolled_back is False         # brew rollback unsupported
    assert "brew" in (res.error or "").lower()


def test_run_update_refuses_dev(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, method="dev")
    res = U.run_update("b" * 40)
    assert res.ok is False
    assert "dev" in (res.error or "").lower() or "unknown" in (res.error or "").lower()


def test_run_update_rejects_concurrent(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path)
    fd = U._acquire_lock()
    try:
        res = U.run_update("b" * 40)
        assert res.ok is False
        assert "in progress" in (res.error or "").lower()
    finally:
        U._release_lock(fd)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_run_update.py -v`
Expected: FAIL — `AttributeError: ... 'run_update'`.

- [ ] **Step 3: Write the minimal implementation (append to `update.py`)**

```python
def _install_cmd(method: InstallMethod, sha: str) -> list[str]:
    base = REPO_URL[:-4] if REPO_URL.endswith(".git") else REPO_URL  # strip .git
    if method == "uv":
        return ["uv", "tool", "install", "--force", "--reinstall",
                f"git+{base}@{sha}"]
    if method == "brew":
        return ["brew", "upgrade", "--fetch-HEAD", "mit112/bridge/bridge"]
    raise ValueError(f"no install command for method {method!r}")


def _install_env(method: InstallMethod) -> dict[str, str]:
    env = dict(os.environ)
    if method == "brew":
        env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
        env["HOMEBREW_NO_INSTALL_CLEANUP"] = "1"
    return env


def _run_installer(cmd: list[str], env: dict[str, str], log_path: Path) -> int:
    """Run the installer non-interactively, appending stdout+stderr to the log."""
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"\n$ {' '.join(cmd)}\n")
        fh.flush()
        proc = subprocess.run(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, check=False)
    return proc.returncode


def _verify_fresh_pid(expected_sha: str) -> bool:
    """Run a FRESH `bridge --version` and confirm it reports `expected_sha`.

    A fresh process is the point: a still-running old panel would report the old
    SHA, so we spawn the installed console script rather than reading our own
    imported `_build`."""
    exe = shutil.which("bridge")
    if exe is None:
        return False
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True,
                              text=True, check=False, timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and (
        expected_sha in proc.stdout or expected_sha[:12] in proc.stdout
    )


def run_update(target_sha: str) -> UpdateResult:
    """Install `target_sha` as a lockfile-guarded transaction.

    Records previous/attempted SHA, method, times, exit, log path; verifies a
    fresh PID reports the target SHA; rolls back (uv) or prints recovery (brew)
    on mismatch."""
    method = install_method()
    started = _now_iso()
    log_path = _update_dir() / "update.log"

    def result(ok, exit_status, error, ended=None, rolled_back=False,
               previous=None):
        return UpdateResult(
            ok=ok, previous_sha=previous, attempted_sha=target_sha, method=method,
            started_at=started, ended_at=ended or _now_iso(),
            exit_status=exit_status, log_path=str(log_path), error=error,
            rolled_back=rolled_back,
        )

    if method in ("dev", "unknown"):
        return result(False, None,
                      f"cannot update a {method} install; use git/pip directly")

    fd = _acquire_lock()
    if fd is None:
        return result(False, None, "an update is already in progress")

    previous = installed_sha()
    try:
        code = _run_installer(_install_cmd(method, target_sha),
                              _install_env(method), log_path)
        if code != 0:
            return result(False, code, f"installer exited {code}",
                          previous=previous)
        if _verify_fresh_pid(target_sha):
            return result(True, code, None, previous=previous)
        # Mismatch: the freshly installed process does not report target_sha.
        if method == "uv" and previous is not None:
            rb = _run_installer(_install_cmd("uv", previous),
                                _install_env("uv"), log_path)
            return result(False, code,
                          f"post-install SHA mismatch; rolled back to {previous[:12]} "
                          f"(rollback exit {rb})",
                          rolled_back=(rb == 0), previous=previous)
        return result(False, code,
                      "post-install SHA mismatch; brew rollback is unsupported -- "
                      "recover with: brew uninstall bridge && brew install --HEAD "
                      "mit112/bridge/bridge",
                      previous=previous)
    finally:
        _release_lock(fd)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_run_update.py -v`
Expected: PASS (all five).

- [ ] **Step 5: Commit**

```bash
git add src/bridge/update.py tests/test_run_update.py
git commit -m "feat: run_update transaction with fresh-PID verify and rollback"
```

---

### Task 6: `bridge --version`/`status` show SHA + method; `bridge update` subcommand

**Files:**
- Modify: `src/bridge/cli.py:324-396` (parser), `:280-301` (`cmd_status`), `:399-406` (`HANDLERS`)
- Test: `tests/test_cli_update.py`

**Interfaces:**
- Consumes: `update.installed_sha`, `update.install_method`, `update.run_update`, `update.UpdateResult` (Tasks 1/2/5).
- Produces:
  - `cmd_update(args, cfg) -> int` — resolves the SHA to install (from the panel's surfaced state via `/api/diagnostics`, else `resolve_remote_sha()`), calls `run_update`, prints outcome, returns 0 on success / 1 on failure.
  - `--version` output becomes `bridge <ver> (<short-sha> <method>)`; `bridge status` gains a `build:` line.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_update.py
import bridge.cli as cli
import bridge.update as U
from bridge.config import load


def test_version_shows_sha_and_method(monkeypatch, capsys):
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    monkeypatch.setattr(U, "install_method", lambda: "uv")
    rc = cli.main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "aaaaaaaaaaaa" in out       # 12-char short SHA
    assert "uv" in out


def test_update_success_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(U, "resolve_remote_sha", lambda **k: "b" * 40)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    monkeypatch.setattr(U, "run_update", lambda sha: U.UpdateResult(
        ok=True, previous_sha="a" * 40, attempted_sha=sha, method="uv",
        started_at="t", ended_at="t", exit_status=0, log_path="/l",
        error=None, rolled_back=False))
    rc = cli.main(["update"])
    assert rc == 0
    assert "updated" in capsys.readouterr().err.lower()


def test_update_failure_exits_one(monkeypatch, capsys):
    monkeypatch.setattr(U, "resolve_remote_sha", lambda **k: "b" * 40)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    monkeypatch.setattr(U, "run_update", lambda sha: U.UpdateResult(
        ok=False, previous_sha="a" * 40, attempted_sha=sha, method="brew",
        started_at="t", ended_at="t", exit_status=1, log_path="/l",
        error="installer exited 1", rolled_back=False))
    rc = cli.main(["update"])
    assert rc == 1
    assert "installer exited 1" in capsys.readouterr().err


def test_update_current_is_noop(monkeypatch, capsys):
    monkeypatch.setattr(U, "resolve_remote_sha", lambda **k: "a" * 40)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    rc = cli.main(["update"])
    assert rc == 0
    assert "up to date" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_update.py -v`
Expected: FAIL — `--version` output has no SHA; no `update` subcommand.

- [ ] **Step 3: Implement `cmd_update` and wire the parser (edit `cli.py`)**

Replace the `--version` action and add the subcommand + handler. In `build_parser`:

```python
    # `--version` now carries the build SHA and install method so a bug report
    # names the exact commit. Imported lazily inside the version string is not
    # possible (argparse needs it at parse build), so import at module top.
    from bridge import update as _u
    _sha = _u.installed_sha()
    _short = _sha[:12] if _sha else "dev"
    parser.add_argument("--version", action="version",
                        version=f"bridge {__version__} ({_short} {_u.install_method()})")
```

Add the subcommand near the others:

```python
    up = sub.add_parser("update", help="update Bridge to the latest main HEAD")
    up.add_argument("--project")
```

Add the handler function:

```python
def cmd_update(args, cfg) -> int:
    """Resolve the SHA the check surfaced (or the remote HEAD) and install it.

    Prints to stderr like `handoff`/`launch`: nothing parses this stdout."""
    from bridge import update

    installed = update.installed_sha()
    # Prefer the SHA the panel already surfaced so the CLI and button install the
    # SAME commit; fall back to a fresh resolve when the panel is down.
    target = None
    try:
        status, body = _request("GET", f"{_base(cfg)}/api/diagnostics")
        if 200 <= status < 300 and isinstance(body, dict):
            upd = body.get("update") or {}
            if upd.get("state") == "behind" and upd.get("latest_sha"):
                target = upd["latest_sha"]
    except Exception:  # noqa: BLE001 - panel down is fine; resolve directly
        pass
    if target is None:
        target = update.resolve_remote_sha()
    if target is None:
        print("bridge update: could not determine the latest commit "
              "(network error?); nothing was changed", file=sys.stderr)
        return 1
    if target == installed:
        print(f"bridge update: already up to date ({target[:12]})",
              file=sys.stderr)
        return 0

    print(f"bridge update: {(installed or 'dev')[:12]} -> {target[:12]} ...",
          file=sys.stderr)
    result = update.run_update(target)
    if result.ok:
        print(f"bridge: updated to {target[:12]} (log: {result.log_path})",
              file=sys.stderr)
        return 0
    print(f"bridge update: FAILED: {result.error} (log: {result.log_path})",
          file=sys.stderr)
    return 1
```

Register it in `HANDLERS`:

```python
HANDLERS = {
    "handoff": cmd_handoff,
    "launch": cmd_launch,
    "next": cmd_next,
    "status": cmd_status,
    "update": cmd_update,
    "diagnose": cmd_diagnose,
    "open": cmd_open,
}
```

Add a `build:` line to `cmd_status` after the `version:` line:

```python
    from bridge import update
    _sha = update.installed_sha()
    print(f"version: {__version__}")
    print(f"build:   {(_sha[:12] if _sha else 'dev')} ({update.install_method()})")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_update.py tests/test_cli.py -v`
Expected: PASS (new tests green; existing CLI tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/bridge/cli.py tests/test_cli_update.py
git commit -m "feat: bridge update subcommand and SHA/method in version+status"
```

---

### Task 7: Bounded update-check worker (isolated from the refresh loop)

**Files:**
- Modify: `src/bridge/update.py` (add `UpdateChecker`)
- Modify: `src/bridge/config.py:210-233` (add `update_check_enabled: bool`), `:115-172` (`_read_config_file` `[update]` table), `:235-268` (`load`)
- Test: `tests/test_update_checker.py`, `tests/test_config.py` (opt-out)

**Interfaces:**
- Consumes: `installed_sha`, `resolve_remote_sha`, `classify`, `UpdateState`, `write_update_state`, `_now_iso` (Tasks 2/3/4).
- Produces:
  - `class UpdateChecker` with `__init__(self, *, enabled: bool, interval_s: float = 1800.0, resolve_fn=resolve_remote_sha)`, `check_once() -> UpdateState`, `snapshot() -> UpdateState`, `run_periodic(stop_event: threading.Event) -> None`.
  - `Config.update_check_enabled: bool` (default `True`).
  - `_ensure_cache_repo(timeout: float = 8.0) -> Path | None` — best-effort `git init --bare` + `git fetch <REPO_URL> main` into `~/.bridge/update/repo.git`; returns the repo path if usable else `None`, never raises. **`_is_ancestor` (Task 3) is rewired to call it** so the `behind` ancestry check actually has objects to reason over. Without this the cache repo is never populated and `classify` can never return `behind` — the nudge would be dead in production (caught in Task 3 review).

**Wire the ancestry cache repo FIRST (Steps A1–A3), then build the checker (Steps 1–5).**

- [ ] **Step A1: Failing test for the fail-closed guard**

```python
# add to tests/test_update_checker.py
def test_ensure_cache_repo_none_without_git(monkeypatch):
    monkeypatch.setattr(U.shutil, "which", lambda _: None)
    assert U._ensure_cache_repo() is None
```

Run: `uv run pytest tests/test_update_checker.py::test_ensure_cache_repo_none_without_git -v`
Expected: FAIL — `AttributeError: ... '_ensure_cache_repo'`.

- [ ] **Step A2: Add `_ensure_cache_repo` and route `_is_ancestor` through it (edit `src/bridge/update.py`)**

Add this function next to `_is_ancestor`:

```python
def _ensure_cache_repo(timeout: float = 8.0) -> Path | None:
    """Best-effort: create the bare ancestry cache and fetch `main` into it so
    `_is_ancestor` has objects to reason over. Returns the repo path if usable,
    else None. Never raises -- a failed fetch just leaves ancestry indeterminate,
    which classify treats as fail-closed `unknown`. Runs only on the bounded
    update-check worker, so a slow fetch never stalls the refresh loop."""
    git = shutil.which("git")
    if git is None:
        return None
    repo = _update_cache_repo()
    try:
        if not (repo / "HEAD").exists():
            repo.mkdir(parents=True, exist_ok=True)
            subprocess.run([git, "init", "--quiet", "--bare", str(repo)],
                           capture_output=True, text=True, check=False, timeout=timeout)
        subprocess.run([git, "-C", str(repo), "fetch", "--quiet", REPO_URL,
                        "+refs/heads/main:refs/heads/main"],
                       capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return repo if (repo / "HEAD").exists() else None
```

Then change `_is_ancestor`'s guard to populate before checking — replace:

```python
    git = shutil.which("git")
    repo = _update_cache_repo()
    if git is None or not repo.is_dir():
        return None
```

with:

```python
    git = shutil.which("git")
    repo = _ensure_cache_repo()
    if git is None or repo is None:
        return None
```

- [ ] **Step A3: Run the new test + the full update suite (no regression)**

Run: `uv run pytest tests/test_update_checker.py::test_ensure_cache_repo_none_without_git tests/test_update*.py -v`
Expected: the new test passes; the Task 3 classify tests still inject `is_ancestor=` (never hit the network) and the checker tests monkeypatch `classify` (never hit it either), so the whole suite stays green with no network access.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_update_checker.py
import threading

import bridge.update as U


def test_check_once_behind(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    monkeypatch.setattr(U, "classify", lambda i, r: "behind")
    ck = U.UpdateChecker(enabled=True, resolve_fn=lambda **k: "b" * 40)
    st = ck.check_once()
    assert st.state == "behind"
    assert st.latest_sha == "b" * 40
    assert st.installed_sha == "a" * 40
    assert st.error is None
    assert st.checked_at is not None


def test_check_once_fail_closed_keeps_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    ck = U.UpdateChecker(enabled=True, resolve_fn=lambda **k: "b" * 40)
    monkeypatch.setattr(U, "classify", lambda i, r: "behind")
    ck.check_once()                                   # prime a good result
    ck2 = U.UpdateChecker(enabled=True, resolve_fn=lambda **k: None)  # network fails
    st = ck2.check_once()
    assert st.state == "stale"                        # never "behind" on failure
    assert st.error is not None
    assert st.latest_sha == "b" * 40                  # last known kept


def test_disabled_never_calls_network(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    called = []
    ck = U.UpdateChecker(enabled=False,
                         resolve_fn=lambda **k: called.append(1) or "b" * 40)
    st = ck.check_once()
    assert called == []
    assert st.state == "unknown"


def test_run_periodic_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    monkeypatch.setattr(U, "classify", lambda i, r: "current")
    ck = U.UpdateChecker(enabled=True, interval_s=0.01,
                         resolve_fn=lambda **k: "a" * 40)
    stop = threading.Event()
    t = threading.Thread(target=ck.run_periodic, args=(stop,))
    t.start()
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_update_checker.py -v`
Expected: FAIL — `AttributeError: ... 'UpdateChecker'`.

- [ ] **Step 3: Implement `UpdateChecker` (append to `update.py`; add `import threading`, `import random`)**

```python
import random
import threading


class UpdateChecker:
    """A bounded worker that polls the remote SHA on a ~30-min cache.

    Kept OFF the 15s refresh loop: `git ls-remote` can hang on a bad network,
    and this must never stall indexing or shutdown. Fails closed -- a failed
    check keeps the last known result as `stale` and never infers `behind`."""

    def __init__(self, *, enabled: bool, interval_s: float = 1800.0,
                 resolve_fn=resolve_remote_sha) -> None:
        self.enabled = enabled
        self.interval_s = interval_s
        self._resolve_fn = resolve_fn
        self._lock = threading.Lock()
        self._state = read_update_state() or UpdateState(
            state="unknown", installed_sha=installed_sha(),
            latest_sha=None, checked_at=None, error=None)

    def snapshot(self) -> UpdateState:
        with self._lock:
            return self._state

    def check_once(self) -> UpdateState:
        installed = installed_sha()
        if not self.enabled:
            state = UpdateState(state="unknown", installed_sha=installed,
                                latest_sha=None, checked_at=_now_iso(), error=None)
            self._store(state)
            return state
        try:
            remote = self._resolve_fn(timeout=8.0)
        except Exception as exc:  # noqa: BLE001 - fail closed on anything
            remote = None
            err = f"{type(exc).__name__}: {exc}"
        else:
            err = None if remote is not None else "could not reach GitHub"
        with self._lock:
            last = self._state
        if remote is None:
            # Fail closed: keep the last known SHA, mark stale, never nudge.
            state = UpdateState(state="stale", installed_sha=installed,
                                latest_sha=last.latest_sha,
                                checked_at=_now_iso(), error=err)
        else:
            state = UpdateState(state=classify(installed, remote),
                                installed_sha=installed, latest_sha=remote,
                                checked_at=_now_iso(), error=None)
        self._store(state)
        return state

    def _store(self, state: UpdateState) -> None:
        with self._lock:
            self._state = state
        try:
            write_update_state(state)
        except OSError:
            log.warning("could not persist update state")

    def run_periodic(self, stop_event: threading.Event) -> None:
        # Jittered so a fleet of installs does not hammer GitHub in lockstep.
        while True:
            self.check_once()
            wait = self.interval_s + random.uniform(0, self.interval_s * 0.1)
            if stop_event.wait(wait):
                return
```

- [ ] **Step 4: Add the opt-out to `config.py`**

In `Config` add `update_check_enabled: bool`. In `load`'s `Config(...)` add `update_check_enabled=True`. In `_read_config_file` parse an `[update]` table:

```python
    update = data.get("update", {})
    if not isinstance(update, dict):
        raise ConfigError(f"{path}: [update] must be a table")
    if "enabled" in update:
        enabled = update["enabled"]
        if not isinstance(enabled, bool):
            raise ConfigError(f"{path}: update.enabled must be true or false")
        values["update_check_enabled"] = enabled
```

Add to `tests/test_config.py`:

```python
def test_update_check_opt_out(tmp_path, monkeypatch):
    from bridge.config import load
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[update]\nenabled = false\n")
    monkeypatch.setenv("BRIDGE_CONFIG", str(cfg_file))
    assert load().update_check_enabled is False


def test_update_check_default_on(tmp_path, monkeypatch):
    from bridge.config import load
    monkeypatch.setenv("BRIDGE_CONFIG", str(tmp_path / "missing.toml"))
    assert load().update_check_enabled is True
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_update_checker.py tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bridge/update.py src/bridge/config.py tests/test_update_checker.py tests/test_config.py
git commit -m "feat: bounded update-check worker with fail-closed cache and opt-out"
```

---

### Task 8: Surface `update` on the status API + start the worker in serve

**Files:**
- Modify: `src/bridge/api.py:513-517` (`create_app` signature — accept `update_checker`), `:815-841` (`_diagnostics`)
- Modify: `src/bridge/__main__.py:156-185` (start the checker thread; join it on shutdown)
- Test: `tests/test_api_update_status.py`, `tests/test_serve_wiring.py`

**Interfaces:**
- Consumes: `update.UpdateChecker`, `update.UpdateState`, `config.Config.update_check_enabled` (Task 7).
- Produces:
  - `create_app(..., update_checker: "UpdateChecker | None" = None)`; when `None`, a disabled checker is constructed so route tests never hit the network.
  - `GET /api/diagnostics` gains `"update": {"state","installed_sha","latest_sha","checked_at","error"}` = `asdict(update_checker.snapshot())`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_update_status.py
from fastapi.testclient import TestClient

from bridge.api import create_app
from bridge.config import load
from bridge.store import Store
from bridge.update import UpdateChecker, UpdateState


def _client(tmp_path, checker):
    projects = tmp_path / "p"; projects.mkdir()
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "s",
                "claude_projects_dir": projects})
    store = Store(cfg.db_path)
    return TestClient(create_app(store, cfg, update_checker=checker)), store


def test_diagnostics_carries_update_object(tmp_path, monkeypatch):
    ck = UpdateChecker(enabled=False)
    monkeypatch.setattr(ck, "snapshot", lambda: UpdateState(
        state="behind", installed_sha="a" * 40, latest_sha="b" * 40,
        checked_at="2026-08-08T00:00:00+00:00", error=None))
    c, store = _client(tmp_path, ck)
    body = c.get("/api/diagnostics").json()
    assert body["update"] == {
        "state": "behind", "installed_sha": "a" * 40, "latest_sha": "b" * 40,
        "checked_at": "2026-08-08T00:00:00+00:00", "error": None}
    store.close()


def test_diagnostics_update_defaults_unknown(tmp_path):
    # No checker passed -> a disabled checker -> unknown, never a network call.
    c, store = _client(tmp_path, None)
    body = c.get("/api/diagnostics").json()
    assert body["update"]["state"] == "unknown"
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_update_status.py -v`
Expected: FAIL — `create_app() got an unexpected keyword argument 'update_checker'`.

- [ ] **Step 3: Implement (edit `api.py`)**

Add the parameter and store it on app state:

```python
def create_app(
    store: Store, cfg: Config, launch_fn: LaunchFn = launcher.launch,
    refresh_coordinator: RefreshCoordinator | None = None,
    notifier: ChangeNotifier | None = None,
    update_checker: "update.UpdateChecker | None" = None,
) -> FastAPI:
```

Near the other `app.state` defaults (after the notifier block ~line 592):

```python
    from bridge import update
    if update_checker is None:
        # A disabled checker never touches the network, so route tests and a
        # panel built directly both get a valid `update` object for free.
        update_checker = update.UpdateChecker(enabled=cfg.update_check_enabled)
    app.state.update_checker = update_checker
```

In `_diagnostics` add the key (import `asdict` is already imported at module top):

```python
            "queued_handoffs": store.queued_handoff_count(),
            "update": asdict(update_checker.snapshot()),
        }
```

- [ ] **Step 4: Start the worker in `__main__.py` (edit run_db_command)**

After the refresh/scheduler threads start (~line 174), before `uvicorn.run`:

```python
    from bridge import update as update_mod

    update_checker = update_mod.UpdateChecker(enabled=cfg.update_check_enabled)
    update_thread = threading.Thread(
        target=update_checker.run_periodic, args=(stop,), daemon=True)
    update_thread.start()
```

Pass it to `create_app`:

```python
        uvicorn.run(
            create_app(store, cfg, refresh_coordinator=refresh_coordinator,
                       notifier=notifier, update_checker=update_checker),
            host="127.0.0.1", port=cfg.port,
        )
```

Add the join to `_shutdown_scheduler` call site — extend the finally block to join `update_thread` (a daemon; a `join(timeout=5)` keeps shutdown bounded even if `git ls-remote` is mid-flight):

```python
    finally:
        watcher.stop()
        update_thread.join(timeout=5.0)
        _shutdown_scheduler(stop, t, store, refresh_thread, watcher=watcher)
```

- [ ] **Step 5: Add a serve-wiring assertion to `tests/test_serve_wiring.py`**

```python
def test_serve_starts_update_checker_thread(monkeypatch):
    # The checker runs on its OWN thread, not the refresh loop.
    import bridge.__main__ as M
    seen = {}
    real_thread = M.threading.Thread
    def spy(*a, **k):
        tgt = k.get("target")
        if tgt is not None and getattr(tgt, "__self__", None).__class__.__name__ == "UpdateChecker":
            seen["update"] = True
        return real_thread(*a, **k)
    monkeypatch.setattr(M.threading, "Thread", spy)
    # (Structure per existing serve-wiring tests: stub uvicorn.run and drive
    # run_db_command(["serve"]) with a hermetic cfg, then assert seen["update"].)
```

Follow the existing `test_serve_wiring.py` harness (it already stubs `uvicorn.run` and builds a hermetic cfg) to invoke `run_db_command(["serve"])` and assert `seen["update"] is True`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_update_status.py tests/test_serve_wiring.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/bridge/api.py src/bridge/__main__.py tests/test_api_update_status.py tests/test_serve_wiring.py
git commit -m "feat: surface update state on status API and run checker in serve"
```

---

### Task 9: Per-install bearer token (0600 file) injected into the panel page

**Files:**
- Modify: `src/bridge/update.py` (token read/create)
- Modify: `src/bridge/api.py` (inject token into the base template context)
- Modify: `src/bridge/templates/base.html` (expose token to JS as a meta tag)
- Test: `tests/test_update_token.py`

**Interfaces:**
- Produces:
  - `read_or_create_token() -> str` — reads `~/.bridge/update/token`, creating a 32-byte urlsafe token at mode `0600` on first call; stable thereafter.
  - Panel exposes it as `<meta name="bridge-update-token" content="...">` for same-origin JS only (never sent off-box).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_update_token.py
import os
import stat

import bridge.update as U


def test_token_created_0600_and_stable(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    t1 = U.read_or_create_token()
    assert len(t1) >= 32
    mode = stat.S_IMODE(os.stat(tmp_path / "token").st_mode)
    assert mode == 0o600
    assert U.read_or_create_token() == t1   # stable across calls


def test_token_meta_in_panel(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from bridge.api import create_app
    from bridge.config import load
    from bridge.store import Store
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    projects = tmp_path / "p"; projects.mkdir()
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "s",
                "claude_projects_dir": projects})
    store = Store(cfg.db_path)
    c = TestClient(create_app(store, cfg))
    html = c.get("/").text
    assert 'name="bridge-update-token"' in html
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_update_token.py -v`
Expected: FAIL — `AttributeError: ... 'read_or_create_token'`.

- [ ] **Step 3: Implement the token (append to `update.py`; add `import secrets`)**

```python
import secrets


def _token_path() -> Path:
    return _update_dir() / "token"


def read_or_create_token() -> str:
    """A per-install bearer token, created 0600 on first read.

    This -- not the loopback Host check -- is what stops CSRF against the update
    endpoint: a cross-site page cannot read this file, so it cannot mint the
    header. The panel injects it into its own same-origin page only."""
    path = _token_path()
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
    token = secrets.token_urlsafe(32)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    os.chmod(path, 0o600)
    return token
```

- [ ] **Step 4: Inject into the panel (edit `api.py` + `base.html`)**

In `api.py`, expose the token as a Jinja global (near `templates.env.globals["shell_freshness"]`, ~line 630):

```python
    from bridge import update
    templates.env.globals["update_token"] = update.read_or_create_token
```

In `src/bridge/templates/base.html`, inside `<head>`:

```html
    <meta name="bridge-update-token" content="{{ update_token() }}">
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_update_token.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bridge/update.py src/bridge/api.py src/bridge/templates/base.html tests/test_update_token.py
git commit -m "feat: per-install bearer token injected into the panel page"
```

---

### Task 10: `POST /api/update` — CSRF-guarded one-click endpoint

**Files:**
- Modify: `src/bridge/api.py` (new route + `UpdateIn` model)
- Test: `tests/test_api_update_endpoint.py`

**Interfaces:**
- Consumes: `update.read_or_create_token`, `update.run_update`, `update.UpdateResult`, `app.state.update_checker` (Tasks 5/7/9).
- Produces:
  - `POST /api/update`, body `{"target_sha": "<40-hex>"}`. Guards, in order: (1) `Authorization: Bearer <token>` must equal the install token (compared with `secrets.compare_digest`); (2) `Sec-Fetch-Site` must be `same-origin` or `none` (reject `cross-site`/`same-site`); (3) the surfaced state must be `behind` and `target_sha` must equal the surfaced `latest_sha` (never a re-resolved `@main`). Returns `403` on guard failure, `409` on SHA mismatch, `200` with `asdict(UpdateResult)` otherwise. (The existing `_same_origin_writes_only` middleware already enforces the `Origin` check on every unsafe method — invariant 3's `Origin` leg — so this route adds token + `Sec-Fetch-Site` + exact-SHA on top.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_update_endpoint.py
from fastapi.testclient import TestClient

import bridge.update as U
from bridge.api import create_app
from bridge.config import load
from bridge.store import Store


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path / "u")
    (tmp_path / "u").mkdir()
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    projects = tmp_path / "p"; projects.mkdir()
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "s",
                "claude_projects_dir": projects})
    store = Store(cfg.db_path)
    ck = U.UpdateChecker(enabled=False)
    monkeypatch.setattr(ck, "snapshot", lambda: U.UpdateState(
        state="behind", installed_sha="a" * 40, latest_sha="b" * 40,
        checked_at="t", error=None))
    app = create_app(store, cfg, update_checker=ck)
    return TestClient(app), store, U.read_or_create_token()


def test_rejects_missing_token(tmp_path, monkeypatch):
    c, store, _ = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 403
    store.close()


def test_rejects_wrong_token(tmp_path, monkeypatch):
    c, store, _ = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": "Bearer WRONG",
                        "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 403
    store.close()


def test_rejects_cross_site(tmp_path, monkeypatch):
    c, store, tok = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    store.close()


def test_rejects_cross_origin_header(tmp_path, monkeypatch):
    # The existing Origin middleware fires before the route body.
    c, store, tok = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "same-origin",
                        "Origin": "http://evil.example:8787",
                        "Host": "127.0.0.1"})
    assert r.status_code == 403
    store.close()


def test_rejects_sha_not_surfaced(tmp_path, monkeypatch):
    c, store, tok = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "c" * 40},  # not latest_sha
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 409
    store.close()


def test_accepts_valid_request(tmp_path, monkeypatch):
    c, store, tok = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(U, "run_update", lambda sha: U.UpdateResult(
        ok=True, previous_sha="a" * 40, attempted_sha=sha, method="uv",
        started_at="t", ended_at="t", exit_status=0, log_path="/l",
        error=None, rolled_back=False))
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["attempted_sha"] == "b" * 40
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_update_endpoint.py -v`
Expected: FAIL — 404 (route absent).

- [ ] **Step 3: Implement the route (edit `api.py`; add `import secrets` at top, and `from bridge import update`)**

Add a model near the other `BaseModel`s:

```python
class UpdateIn(BaseModel):
    target_sha: str

    @field_validator("target_sha")
    @classmethod
    def _forty_hex(cls, v: str) -> str:
        if len(v) != 40 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("target_sha must be a 40-char lowercase hex SHA")
        return v
```

Add the route (near the other `@app.post` routes):

```python
    @app.post("/api/update")
    def api_update(request: Request, payload: UpdateIn):
        # 1) Per-install bearer token. `compare_digest` avoids a timing oracle.
        expected = update.read_or_create_token()
        auth = request.headers.get("authorization", "")
        presented = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not presented or not secrets.compare_digest(presented, expected):
            log.warning("refused /api/update: bad or missing token")
            raise HTTPException(status_code=403, detail="bad update token")
        # 2) Sec-Fetch-Site: a browser sets this and a page cannot forge it.
        #    Absent (a server-side client like the CLI) stays allowed; a
        #    cross-site/same-site value is refused. (Origin is already checked
        #    by `_same_origin_writes_only` for every unsafe method.)
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            log.warning("refused /api/update: Sec-Fetch-Site=%r", site)
            raise HTTPException(status_code=403, detail="cross-site update refused")
        # 3) Install ONLY the exact SHA the check surfaced -- never a re-resolved
        #    @main. A mismatch means the panel's offer and the request disagree.
        snap = request.app.state.update_checker.snapshot()
        if snap.state != "behind" or payload.target_sha != snap.latest_sha:
            raise HTTPException(status_code=409,
                                detail="target SHA is not the currently offered update")
        return asdict(update.run_update(payload.target_sha))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_update_endpoint.py -v`
Expected: PASS (all six).

- [ ] **Step 5: Commit**

```bash
git add src/bridge/api.py tests/test_api_update_endpoint.py
git commit -m "feat: CSRF-guarded POST /api/update installing the exact offered SHA"
```

---

### Task 11: Panel banner — per-SHA dismissal, confirm dialog, copy-command fallback

**Files:**
- Modify: `src/bridge/templates/base.html` (banner markup in the shell)
- Create: `src/bridge/static/update.js`
- Modify: `src/bridge/templates/base.html` (script include) / static registration
- Test: `tests/test_shell_contract.py` (banner presence + copy command), `tests/js/` (dismissal keyed by SHA)

**Interfaces:**
- Consumes: `GET /api/diagnostics` `update` object (Task 8); `<meta name="bridge-update-token">` (Task 9); `POST /api/update` (Task 10).
- Produces: a dismissible banner that renders only when `update.state === "behind"`; dismissal stored in `localStorage` under key `bridge:update-dismissed:<latest_sha>` (so dismissing SHA A never suppresses SHA B); a confirmation dialog showing `installed_sha[:12] → latest_sha[:12]`; a copy-able `bridge update` command always visible.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_shell_contract.py
def test_update_banner_scaffold_and_copy_command(client):
    c, _, _ = client
    html = c.get("/").text
    assert 'id="update-banner"' in html            # present but hidden until behind
    assert "bridge update" in html                  # copy-able fallback command
    assert 'name="bridge-update-token"' in html     # token available to the JS
```

For the JS behavior, add a static-JS assertion mirroring existing `tests/test_static_js.py` (which reads the served file and asserts on content):

```python
# add to tests/test_static_js.py
def test_update_js_keys_dismissal_by_sha(client):
    c, _, _ = client
    js = c.get("/static/update.js").text
    assert "bridge:update-dismissed:" in js         # dismissal keyed by SHA
    assert "latest_sha" in js
    assert "/api/update" in js
    assert "Authorization" in js and "Bearer" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_shell_contract.py -k update_banner tests/test_static_js.py -k update_js -v`
Expected: FAIL — banner id / update.js absent.

- [ ] **Step 3: Add the banner markup to `base.html`**

Inside the shell chrome, near the top of `<body>` (adjust to the existing shell structure):

```html
<div id="update-banner" hidden role="status" aria-live="polite">
  <span class="update-banner__text">
    A newer Bridge is available (<code id="update-banner__from"></code> →
    <code id="update-banner__to"></code>).
  </span>
  <button type="button" id="update-banner__apply">Update now</button>
  <button type="button" id="update-banner__dismiss" aria-label="Dismiss">×</button>
  <span class="update-banner__fallback">
    or run <code>bridge update</code>
  </span>
</div>
<script src="/static/update.js" defer></script>
```

- [ ] **Step 4: Write `src/bridge/static/update.js`**

```javascript
// Update banner: renders only when the status API reports state "behind".
// Dismissal is keyed by the offered SHA so dismissing one offer never hides a
// later one. `bridge update` is always shown as the safe copy-able fallback.
(function () {
  "use strict";
  var banner = document.getElementById("update-banner");
  if (!banner) return;
  var token = (document.querySelector('meta[name="bridge-update-token"]') || {}).content;

  function dismissedKey(sha) { return "bridge:update-dismissed:" + sha; }

  function render(upd) {
    if (!upd || upd.state !== "behind" || !upd.latest_sha) { banner.hidden = true; return; }
    if (localStorage.getItem(dismissedKey(upd.latest_sha))) { banner.hidden = true; return; }
    document.getElementById("update-banner__from").textContent =
      (upd.installed_sha || "dev").slice(0, 12);
    document.getElementById("update-banner__to").textContent = upd.latest_sha.slice(0, 12);
    banner.hidden = false;
    banner.dataset.sha = upd.latest_sha;
    banner.dataset.from = upd.installed_sha || "dev";
  }

  document.getElementById("update-banner__dismiss").addEventListener("click", function () {
    if (banner.dataset.sha) localStorage.setItem(dismissedKey(banner.dataset.sha), "1");
    banner.hidden = true;
  });

  document.getElementById("update-banner__apply").addEventListener("click", function () {
    var sha = banner.dataset.sha;
    if (!confirm("Update Bridge from " + banner.dataset.from.slice(0, 12) +
                 " to " + sha.slice(0, 12) + "? The panel will restart.")) return;
    fetch("/api/update", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
      body: JSON.stringify({ target_sha: sha })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (!res.ok) alert("Update failed: " + (res.error || "unknown") +
                         "\nRun `bridge update` to retry.");
    }).catch(function () {
      alert("Update request failed. Run `bridge update` to retry.");
    });
  });

  function poll() {
    fetch("/api/diagnostics").then(function (r) { return r.json(); })
      .then(function (d) { render(d.update); }).catch(function () {});
  }
  poll();
  setInterval(poll, 60000);
})();
```

- [ ] **Step 5: Register `update.js` in the static dir**

`update.js` lives in `src/bridge/static/`, already served by the mounted `CachedStaticFiles` at `/static` (`api.py:651`). No wheel change needed — `src/` ships whole.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_shell_contract.py -k update_banner tests/test_static_js.py -k update_js -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/bridge/templates/base.html src/bridge/static/update.js tests/test_shell_contract.py tests/test_static_js.py
git commit -m "feat: update banner with per-SHA dismissal, confirm, and copy fallback"
```

---

### Task 12: One-shot LaunchAgent updater + plist re-bootstrap + reconnect state

**Files:**
- Modify: `src/bridge/setup.py` (generate + bootstrap a one-shot updater plist; re-bootstrap the panel plist)
- Modify: `src/bridge/update.py` (`run_update_via_launchagent(target_sha) -> Path` writing the state file the panel reads on reconnect)
- Test: `tests/test_setup.py` (one-shot plist shape), `tests/test_update_launchagent.py`

**Interfaces:**
- Consumes: `_generate_plist` pattern (`setup.py:292`), `LAUNCHD_LABEL`, `LAUNCHD_AGENTS_DIR` (`setup.py:35-45`); `update.write_update_state` (Task 4).
- Produces:
  - `setup.generate_updater_plist(python_path: str, target_sha: str) -> str` — a **one-shot** job (`RunAtLoad` true, **no** `KeepAlive`) with a **distinct label** `dev.bridge.updater`, so `kickstart -k` on `dev.bridge.panel` never kills it.
  - `setup.bootstrap_updater(target_sha: str) -> bool` — writes the plist and `launchctl bootstrap`s it.
  - `update.run_update_via_launchagent(target_sha: str) -> UpdateResult` — the panel-side flow: performs `run_update`, then re-bootstraps the panel plist from the (possibly moved) `sys.executable`, then writes the update-state file the panel reads on SSE reconnect.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_update_launchagent.py
import plistlib

import bridge.setup as S


def test_updater_plist_is_one_shot_with_distinct_label():
    xml = S.generate_updater_plist("/usr/bin/python3", "b" * 40)
    data = plistlib.loads(xml.encode())
    assert data["Label"] == "dev.bridge.updater"
    assert data["Label"] != S.LAUNCHD_LABEL      # not a child of the panel job
    assert data["RunAtLoad"] is True
    assert "KeepAlive" not in data               # one-shot: must not respawn
    argv = data["ProgramArguments"]
    assert "update" in argv and ("b" * 40) in " ".join(argv)


# tests/test_update_launchagent.py (continued)
import bridge.update as U


def test_via_launchagent_writes_reconnect_state(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "run_update", lambda sha: U.UpdateResult(
        ok=True, previous_sha="a" * 40, attempted_sha=sha, method="uv",
        started_at="t", ended_at="t", exit_status=0, log_path="/l",
        error=None, rolled_back=False))
    monkeypatch.setattr(U, "_rebootstrap_panel", lambda: True)
    res = U.run_update_via_launchagent("b" * 40)
    assert res.ok is True
    st = U.read_update_state()
    assert st.installed_sha == "b" * 40 or st.latest_sha == "b" * 40
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_update_launchagent.py -v`
Expected: FAIL — `AttributeError: ... 'generate_updater_plist'` / `'run_update_via_launchagent'`.

- [ ] **Step 3: Implement the updater plist (edit `setup.py`)**

```python
UPDATER_LABEL = "dev.bridge.updater"
UPDATER_PLIST_NAME = f"{UPDATER_LABEL}.plist"


def generate_updater_plist(python_path: str, target_sha: str) -> str:
    """A ONE-SHOT LaunchAgent that runs `bridge update` for an exact SHA.

    Distinct label and no KeepAlive: it is deliberately NOT a child of the panel
    job, because `kickstart -k dev.bridge.panel` during the restart would kill
    the updater mid-flight if it were. RunAtLoad fires it once on bootstrap; it
    exits and stays exited."""
    esc = _xml_escape
    log_path = str(BRIDGE_DIR / "update.log")
    return textwrap.dedent(f"""\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
        <key>Label</key>
        <string>{UPDATER_LABEL}</string>
        <key>ProgramArguments</key>
        <array>
            <string>{esc(python_path)}</string>
            <string>-m</string>
            <string>bridge</string>
            <string>update</string>
            <string>--sha</string>
            <string>{esc(target_sha)}</string>
        </array>
        <key>RunAtLoad</key>
        <true/>
        <key>StandardOutPath</key>
        <string>{esc(log_path)}</string>
        <key>StandardErrorPath</key>
        <string>{esc(log_path)}</string>
    </dict>
    </plist>""")


def bootstrap_updater(target_sha: str) -> bool:
    """Write and bootstrap the one-shot updater job."""
    dest = LAUNCHD_AGENTS_DIR / UPDATER_PLIST_NAME
    uid = os.getuid()
    try:
        LAUNCHD_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        dest.write_text(generate_updater_plist(sys.executable, target_sha),
                        encoding="utf-8")
    except OSError:
        return False
    # Best-effort bootout of a prior run, then bootstrap fresh.
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{UPDATER_LABEL}"],
                   check=False, capture_output=True, text=True)
    proc = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(dest)],
                          check=False, capture_output=True, text=True)
    return proc.returncode == 0
```

Add a `--sha` argument to the `update` subparser (Task 6's parser) so the LaunchAgent can name the exact SHA:

```python
    up.add_argument("--sha", help="install this exact commit (used by the "
                                  "one-shot LaunchAgent updater)")
```

and honor it in `cmd_update`: if `args.sha` is set, use it as `target` directly (skip resolution), still refusing when it equals `installed`.

- [ ] **Step 4: Implement `run_update_via_launchagent` (edit `update.py`)**

```python
def _rebootstrap_panel() -> bool:
    """Re-bootstrap the panel plist from the (possibly moved) sys.executable.

    A uv/brew upgrade can move the interpreter the plist pins by absolute path
    (setup.py:292/:330), so the plist must be regenerated from the NEW path
    before the panel restarts, or launchd relaunches a binary that no longer
    exists."""
    from bridge.setup import run_launchd_only
    try:
        return run_launchd_only() == 0
    except Exception:  # noqa: BLE001 - restart failure must not crash the flow
        log.exception("panel re-bootstrap failed")
        return False


def run_update_via_launchagent(target_sha: str) -> UpdateResult:
    """The panel-side flow: install, re-bootstrap the panel plist, and write the
    reconnect state file so the panel can tell "updated" from "crashed" across
    the SSE reconnect that the restart forces."""
    result = run_update(target_sha)
    if result.ok:
        _rebootstrap_panel()
        write_update_state(UpdateState(
            state="current", installed_sha=target_sha, latest_sha=target_sha,
            checked_at=_now_iso(), error=None))
    else:
        write_update_state(UpdateState(
            state="stale", installed_sha=installed_sha(), latest_sha=target_sha,
            checked_at=_now_iso(), error=result.error))
    return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_update_launchagent.py tests/test_setup.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bridge/setup.py src/bridge/update.py src/bridge/cli.py tests/test_update_launchagent.py
git commit -m "feat: one-shot LaunchAgent updater with plist re-bootstrap and reconnect state"
```

---

### Task 13: Privacy documentation — README + setup note

**Files:**
- Modify: `README.md` (privacy + opt-out section)
- Modify: `src/bridge/setup.py:661-711` (`run_setup` prints the privacy note)
- Test: `tests/test_setup.py` (privacy note printed)

**Interfaces:**
- Consumes: `config.toml` `[update] enabled` (Task 7).
- Produces: user-facing text stating Bridge contacts GitHub via `git ls-remote` to check for updates, collects/stores **no** GitHub token, and can be turned off with `[update]\nenabled = false`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_setup.py
def test_setup_prints_update_privacy_note(monkeypatch, capsys):
    import bridge.setup as S
    # Drive only the privacy-note helper (added below), not the full wizard.
    S._print_update_privacy_note()
    out = capsys.readouterr().out.lower()
    assert "github" in out
    assert "update" in out
    assert "enabled = false" in out
    assert "no" in out and "token" in out    # no token stored
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_setup.py -k privacy -v`
Expected: FAIL — `AttributeError: ... '_print_update_privacy_note'`.

- [ ] **Step 3: Implement the note (edit `setup.py`)**

```python
def _print_update_privacy_note() -> None:
    """State the one network call Bridge makes and how to turn it off."""
    _banner("Update checks")
    print(textwrap.dedent(f"""\
      Bridge checks for updates by asking GitHub for the latest commit on
      `main` (`git ls-remote`). It stores NO GitHub token and sends no data
      about you. Updates install the exact commit surfaced -- never a floating
      ref -- and always offer `bridge update` as a copy-able fallback.

      To turn update checks off, add to {CONFIG_PATH}:

        [update]
        enabled = false"""))
    print()
```

Call it from `run_setup` after `_step_config` (~line 685):

```python
    _print_update_privacy_note()
```

- [ ] **Step 4: Add the README section**

Add under the install/quick-start section:

```markdown
## Staying up to date

Bridge tracks `main`. It checks for updates by asking GitHub for the latest
commit with `git ls-remote` — no GitHub token is collected or stored, and no
data about you is sent. When a newer commit is available the panel shows an
"update available" banner; you can click **Update now** or run:

```bash
bridge update
```

Updates always install the **exact** commit the check surfaced, then verify a
freshly launched process reports it (rolling back on mismatch for `uv`
installs). To disable update checks, add to `~/.bridge/config.toml`:

```toml
[update]
enabled = false
```
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_setup.py -k privacy -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add README.md src/bridge/setup.py tests/test_setup.py
git commit -m "docs: document update-check privacy and the opt-out setting"
```

---

### Task 14: Full-suite green + hermetic recheck

**Files:** none (verification gate)

- [ ] **Step 1: Run the full suite**

Run: `uv run pytest -q`
Expected: all green (existing + new). Investigate any regression before proceeding — do not `-k`-narrow past a failure.

- [ ] **Step 2: Run the hermetic subset the repo defines**

Run: `uv run pytest -q -p no:cacheprovider` (and the project's hermetic marker/target if one exists, per the OSS-readiness memory's "1332/1331-hermetic green").
Expected: green.

- [ ] **Step 3: Confirm no real `~/.bridge` writes occurred**

The autouse `never_touch_the_real_bridge_dir` guard (conftest) covers spool/launcher; the new `update.py` writes under `~/.bridge/update`. Confirm every new test monkeypatches `update._update_dir` to a tmp path (all do above). If any missed it, fix the test.

- [ ] **Step 4: Commit any test fixups**

```bash
git add -A && git commit -m "test: keep update tests hermetic under tmp_path"
```

---

## Self-Review

**1. Spec coverage (Sections B + C):**

| Spec requirement | Task |
|---|---|
| Build-time SHA injected or build fails; dev reports `unknown` (§C) | 1 |
| `bridge --version`/`status` show short SHA + method (§C) | 6 |
| Detect install method (uv/brew/dev/unknown, both Homebrew prefixes, never guess) (§B) | 2 |
| `git ls-remote` remote SHA, fail closed (§C) | 3 |
| classify current/behind/diverged/unknown; behind = FF descendant (§C) | 3 |
| Separate bounded worker, ~30-min cache, jittered backoff, last-success/error timestamps, fail closed (§C) | 7 |
| Privacy/opt-out setting, no token stored (§C) | 7, 13 |
| Transaction: lockfile, prev/attempted SHA, method, times, exit, log (§B, inv. 4) | 4, 5 |
| uv install exact SHA `--force --reinstall`; brew `--fetch-HEAD` + env (§B) | 5 |
| Verify fresh PID reports target SHA; rollback uv / recovery brew (§B) | 5 |
| Status API `update` object (§C) | 8 |
| Per-install bearer token 0600 file, injected into page (inv. 3) | 9 |
| `POST /api/update`: token + Origin + Sec-Fetch-Site + exact-SHA (inv. 3) | 10 (Origin leg via existing middleware) |
| CSRF regression tests (missing/wrong token, cross-origin, Sec-Fetch-Site) (Testing) | 10 |
| Banner: per-SHA dismissal, copy-able `bridge update`, confirm current→new (§C) | 11 |
| One-shot LaunchAgent updater (not panel child), plist re-bootstrap, reconnect state (§B, §C, inv. 4) | 12 |

No B/C requirement is left without a task.

**2. Placeholder scan:** No `TBD`/`TODO`/"handle edge cases". Every code step carries real Python/JS and every test step real assertions. The two spots that lean on existing harnesses (Task 8 Step 5 serve-wiring spy, Task 11 static-JS assertion) name the exact existing file and pattern to follow rather than inventing a new harness.

**3. Type consistency (checked across tasks):**
- `installed_sha() -> str | None`, `install_method() -> InstallMethod`, `resolve_remote_sha(url, ref, timeout) -> str | None`, `classify(installed, remote, *, is_ancestor=...) -> Classification`, `run_update(target_sha) -> UpdateResult` — identical everywhere used (Tasks 2/3/5/6/8/10/12).
- `UpdateState(state, installed_sha, latest_sha, checked_at, error)` and `UpdateResult(ok, previous_sha, attempted_sha, method, started_at, ended_at, exit_status, log_path, error, rolled_back)` — field names/types identical in definition (Task 4) and every construction (Tasks 5/6/7/8/9/10/12) and the `asdict` surface (Tasks 8/10).
- Status `update` object keys match `UpdateState` fields exactly (Task 8 test asserts the full dict).
- `UpdateChecker(enabled=..., interval_s=..., resolve_fn=...)` with `check_once`/`snapshot`/`run_periodic` — consistent in Task 7 and consumed unchanged in Tasks 8/10.
- `_update_dir()` is the single seam every persistence helper (`_lock_path`, `_state_path`, `_token_path`, `_update_cache_repo` via `~/.bridge/update`, logs) routes through, so one monkeypatch makes every test hermetic (Tasks 4/5/7/9/12/14).

**Noted contract adaptation:** `classify` gains a keyword-only `is_ancestor` with a default so the positional `classify(installed, remote)` form in the contract still holds at every call site (recorded in Task 3). No other names diverge from the contract.
