"""The one update primitive the CLI (`bridge update`) and the panel button
(`POST /api/update`) both call.

It never installs the floating `@main`: the check resolves a concrete SHA and
the install pins that exact SHA. The running commit is read from the installer's
PEP 610 `direct_url.json` (git installs), falling back to the `_build` sentinel
that the Homebrew formula stamps."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import logging
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
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
    return sha if _SHA_RE.match(sha) else None


def _update_cache_repo() -> Path:
    """A bare object cache used only to answer ancestry questions offline."""
    return _update_dir() / "repo.git"


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


def _is_ancestor(installed: str, remote: str) -> bool | None:
    """True if `installed` is an ancestor of `remote` (remote is a fast-forward
    descendant). None when it cannot be decided (objects absent / git error) --
    which the classifier treats as fail-closed "unknown"."""
    git = shutil.which("git")
    repo = _ensure_cache_repo()
    if git is None or repo is None:
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_dir() -> Path:
    d = Path.home() / ".bridge" / "update"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock_path() -> Path:
    return _update_dir() / "update.lock"


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
        try:
            code = _run_installer(_install_cmd(method, target_sha),
                                  _install_env(method), log_path)
        except OSError as exc:
            # `uv`/`brew` absent -> FileNotFoundError (an OSError). run_update is
            # the transaction boundary and must ALWAYS return a result, never
            # raise: the launchagent flow still writes reconnect state and the
            # endpoint still returns clean ok=false JSON instead of a 500.
            return result(False, None, f"installer could not be run: {exc}",
                          previous=previous)
        if code != 0:
            return result(False, code, f"installer exited {code}",
                          previous=previous)
        if _verify_fresh_pid(target_sha):
            return result(True, code, None, previous=previous)
        # Mismatch: the freshly installed process does not report target_sha.
        if method == "uv":
            if previous is None:
                return result(False, code,
                              "post-install SHA mismatch and no previous SHA to "
                              "roll back to; reinstall manually: uv tool install "
                              "--force --reinstall git+https://github.com/mit112/"
                              "bridge@<a known good sha>",
                              previous=previous)
            rb = _run_installer(_install_cmd("uv", previous),
                                _install_env("uv"), log_path)
            if rb == 0:
                msg = (f"post-install SHA mismatch; rolled back to "
                      f"{previous[:12]}")
            else:
                msg = (f"post-install SHA mismatch; rollback attempt FAILED "
                      f"(rollback exit {rb}); still on the broken install -- "
                      f"reinstall manually: uv tool install --force --reinstall "
                      f"git+https://github.com/mit112/bridge@{previous}")
            return result(False, code, msg, rolled_back=(rb == 0),
                          previous=previous)
        return result(False, code,
                      "post-install SHA mismatch; brew rollback is unsupported -- "
                      "recover with: brew uninstall bridge && brew install --HEAD "
                      "mit112/bridge/bridge",
                      previous=previous)
    finally:
        _release_lock(fd)


def _rebootstrap_panel() -> bool:
    """Re-bootstrap the panel plist from the (possibly moved) sys.executable.

    A uv/brew upgrade can move the interpreter the plist pins by absolute path
    (setup.py:292/:330), so the plist must be regenerated from the NEW path
    before the panel restarts, or launchd relaunches a binary that no longer
    exists.

    `assume_yes=True` is load-bearing: this runs inside a launchd one-shot job
    with no tty, and the interactive `run_launchd_only` prompt would `sys.exit()`
    on EOF -- a `SystemExit` that a bare `except Exception` cannot catch."""
    from bridge.setup import run_launchd_only
    return run_launchd_only(assume_yes=True) == 0


def is_managed_launchagent() -> bool:
    """True when this panel runs under its own installed LaunchAgent.

    Only then is the async one-shot updater usable: a detached job can install
    the new package AND restart the panel, so `POST /api/update` can return
    immediately and let the reconnect resolve the outcome. A manual `bridge
    serve` (dev/unknown, or no panel plist) has no agent to relaunch it, so the
    endpoint must update in-process instead or it would leave the panel on old
    code with nothing to restart it.

    A monkeypatchable seam for the endpoint's test; reads `install_method()` and
    the panel plist path at call time so both stay overridable."""
    if install_method() not in ("uv", "brew"):
        return False
    from bridge.setup import LAUNCHD_AGENTS_DIR, LAUNCHD_PLIST_NAME
    return (LAUNCHD_AGENTS_DIR / LAUNCHD_PLIST_NAME).exists()


def run_update_via_launchagent(target_sha: str) -> UpdateResult:
    """The panel-side flow: install, re-bootstrap the panel plist, and write the
    reconnect state file so the panel can tell "updated" from "crashed" across
    the SSE reconnect that the restart forces.

    Invoked by the one-shot `dev.bridge.updater` LaunchAgent (`bridge update
    --sha <sha> --via-launchagent`), never by the panel process itself."""
    result = run_update(target_sha)
    if not result.ok:
        write_update_state(UpdateState(
            state="stale", installed_sha=installed_sha(), latest_sha=target_sha,
            checked_at=_now_iso(), error=result.error))
        return result
    # The install landed. Now re-bootstrap the panel plist from the new
    # interpreter path and let launchd relaunch it. The restart MUST NOT be able
    # to skip the state write below: `_rebootstrap_panel` can raise -- including
    # `SystemExit`, which is a BaseException, not an Exception -- and if that
    # escaped, the panel would reconnect to a state file that still said the old
    # SHA and never learn the update succeeded. Catch BaseException, record the
    # failure, and write state either way.
    restart_error = None
    try:
        if not _rebootstrap_panel():
            restart_error = "panel re-bootstrap failed"
    except BaseException as exc:  # noqa: BLE001 - SystemExit must not skip state
        log.exception("panel re-bootstrap raised")
        restart_error = f"panel re-bootstrap raised: {type(exc).__name__}: {exc}"
    write_update_state(UpdateState(
        state="current", installed_sha=target_sha, latest_sha=target_sha,
        checked_at=_now_iso(), error=restart_error))
    if restart_error is not None:
        result = dataclasses.replace(result, error=restart_error)
    return result


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
