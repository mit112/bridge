"""`bridge diagnose`: a read-only snapshot for a bug report.

It opens no database, mutates nothing and makes no network call. The one external
thing it touches is `launchctl list`, and even that is injectable (`run=`) so the
suite need not shell out. `render` takes the serve-log path too, so a test points
it at a tmp file rather than the real `~/.bridge/serve.log`.
"""

import platform
import subprocess
import sys
from pathlib import Path

from bridge import __version__
from bridge.config import config_path

LAUNCH_AGENT_LABEL = "dev.bridge.panel"
DEFAULT_LOG_TAIL = 40


def serve_log_path() -> Path:
    return Path.home() / ".bridge" / "serve.log"


def launch_agent_state(label: str = LAUNCH_AGENT_LABEL, run=subprocess.run) -> str:
    """Report whether the LaunchAgent is loaded, from `launchctl list`.

    `launchctl list` prints `PID\\tStatus\\tLabel` per line; an unloaded agent is
    simply absent, and a loaded-but-not-running one shows `-` for its PID. A
    missing `launchctl` (not macOS) or a non-zero exit both degrade to a single
    "unavailable" line rather than a traceback -- diagnose must never itself be
    the thing that crashes.
    """
    try:
        proc = run(
            ["launchctl", "list"],
            capture_output=True, text=True, check=False,
        )
    except (OSError, ValueError):
        return "launchctl unavailable"
    if proc.returncode != 0:
        return "launchctl unavailable"
    for line in proc.stdout.splitlines():
        parts = line.split()
        if parts and parts[-1] == label:
            pid = parts[0]
            if pid == "-":
                return "loaded (not running)"
            return f"loaded (pid {pid})"
    return "not loaded"


def log_tail(path: Path, n: int = DEFAULT_LOG_TAIL) -> str:
    """The last `n` lines of a log file, or a clear line saying why there are none."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"(no serve log at {path})"
    except OSError as exc:
        return f"(could not read serve log at {path}: {exc})"
    lines = text.splitlines()
    if not lines:
        return f"(serve log at {path} is empty)"
    return "\n".join(lines[-n:])


def render(
    cfg,
    *,
    serve_log: Path | None = None,
    log_lines: int = DEFAULT_LOG_TAIL,
    run=subprocess.run,
    label: str = LAUNCH_AGENT_LABEL,
) -> str:
    """The whole human-readable block, as one string."""
    serve_log = serve_log or serve_log_path()
    config_dir = config_path().parent

    out = [
        "Bridge diagnostics",
        f"  bridge version: {__version__}",
        f"  python:         {platform.python_version()} ({sys.executable})",
        f"  platform:       {platform.platform()}",
        "",
        "Config",
        f"  config dir:   {config_dir}",
        f"  projects dir: {cfg.claude_projects_dir}",
        f"  port:         {cfg.port}",
        "",
        "LaunchAgent",
        f"  {label}: {launch_agent_state(label=label, run=run)}",
        "",
        f"serve.log (last {log_lines} lines of {serve_log})",
        log_tail(serve_log, n=log_lines),
    ]
    return "\n".join(out)
