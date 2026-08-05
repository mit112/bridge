"""Interactive setup for Bridge — one command to go from nothing to running.

`bridge setup` walks a new user through every choice that matters, generates
the config file and LaunchAgent plist with live-resolved paths, copies the
/handoff slash command, and optionally bootstraps the LaunchAgent.

`bridge setup --launchd-only` regenerates just the plist (useful after a
package update if the venv path changed).

`bridge setup --uninstall` tears down the LaunchAgent and offers to remove
~/.bridge/.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

HOME = Path.home()
BRIDGE_DIR = HOME / ".bridge"
CLAUDE_COMMANDS_DIR = HOME / ".claude" / "commands"
HANDOFF_SRC = "commands/handoff.md"
HANDOFF_DEST = CLAUDE_COMMANDS_DIR / "handoff.md"
LAUNCHD_LABEL = "com.mitsheth.bridge-panel"
LAUNCHD_PLIST_NAME = f"{LAUNCHD_LABEL}.plist"
LAUNCHD_PLIST_PATH = BRIDGE_DIR / LAUNCHD_PLIST_NAME
LAUNCHD_AGENTS_DIR = HOME / "Library" / "LaunchAgents"
CONFIG_PATH = BRIDGE_DIR / "config.toml"
DEFAULT_PORT = 8787

def _xml_escape(s: str) -> str:
    """Escape special characters for XML text content."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")


def _toml_str(s: str) -> str:
    """Escape a string for a TOML basic string."""
    return s.replace("\\", "\\\\").replace('"', '\\"')

# Directories we scan for git repos during interactive setup.
COMMON_PROJECT_ROOTS = ["~/dev", "~/projects", "~/src", "~/code", "~/git"]


def _banner(text: str) -> None:
    print(f"\n  {text}")


def _ok(text: str) -> None:
    print(f"  ✓  {text}")


def _info(text: str) -> None:
    print(f"  ℹ  {text}")


def _warn(text: str) -> None:
    print(f"  ⚠  {text}")


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  ?  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Setup cancelled.")
        sys.exit(0)
    if not answer and default is not None:
        return default
    return answer


def _ask_yn(prompt: str, default: bool = True) -> bool:
    yn = "Y/n" if default else "y/N"
    suffix = f" [{yn}]"
    try:
        answer = input(f"  ?  {prompt}{suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Setup cancelled.")
        sys.exit(0)
    if not answer:
        return default
    return answer in ("y", "yes")


def _git_repos_in(dir_path: Path) -> list[Path]:
    """Direct children of `dir_path` that are git repos."""
    if not dir_path.is_dir():
        return []
    return sorted(
        p for p in dir_path.iterdir() if p.is_dir() and (p / ".git").exists()
    )


def _resolve_claude_path() -> str | None:
    """Find `claude` on the user's PATH. Returns the directory containing it."""
    claude = shutil.which("claude")
    if claude is None:
        return None
    return str(Path(claude).parent)


def _port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """True if nothing is listening on `host:port`."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _find_free_port(start: int = DEFAULT_PORT, attempts: int = 20) -> int:
    """Find the first free port starting from `start`."""
    for port in range(start, start + attempts):
        if _port_is_free(port):
            return port
    return start  # fallback


# ── Step: discover project roots ────────────────────────────────────────────


def _step_discovery() -> list[str]:
    """Scan common directories for git repos, let the user pick which to use.

    Returns the list of directory paths the user confirmed.
    """
    _banner("Project directories")

    # Scan all common roots
    found: dict[str, list[Path]] = {}
    for raw in COMMON_PROJECT_ROOTS:
        d = Path(raw).expanduser()
        repos = _git_repos_in(d)
        if repos:
            found[str(d)] = repos

    if not found:
        _info("No git repos found under common directories (~/dev, ~/projects, etc.).")
        custom = _ask("Enter a directory to scan for projects", str(HOME))
        d = Path(custom).expanduser()
        repos = _git_repos_in(d)
        if repos:
            found[str(d)] = repos
        else:
            _warn(f"No git repos found in {d}. You can add paths later in {CONFIG_PATH}.")
            return []

    # Present findings
    print()
    for dir_path, repos in found.items():
        print(f"  {dir_path}/  ({len(repos)} repo{'s' if len(repos) != 1 else ''})")
        # Show up to 5 repo names
        for r in repos[:5]:
            print(f"    - {r.name}")
        if len(repos) > 5:
            print(f"    ... and {len(repos) - 5} more")

    print()
    confirmed = []
    for dir_path in found:
        if _ask_yn(f"Include {dir_path}/?", default=True):
            confirmed.append(dir_path)

    if not confirmed:
        _info("No directories selected. You can add paths later in the config file.")
        _info(f"  {CONFIG_PATH}")
        return []

    # Allow adding a custom path
    while True:
        custom = _ask("Add another directory, or press Enter to continue")
        if not custom:
            break
        d = Path(custom).expanduser()
        if not d.is_dir():
            _warn(f"{d} does not exist or is not a directory.")
            continue
        repos = _git_repos_in(d)
        if not repos:
            _warn(f"No git repos found directly in {d}.")
        confirmed.append(str(d))

    return confirmed


# ── Step: port ──────────────────────────────────────────────────────────────


def _step_port() -> int:
    """Confirm the port. If 8787 is taken, offer alternatives."""
    _banner("Port")
    if _port_is_free(DEFAULT_PORT):
        _ok(f"Port {DEFAULT_PORT} is available.")
        return DEFAULT_PORT

    _warn(f"Port {DEFAULT_PORT} is in use.")
    alt = _find_free_port(DEFAULT_PORT + 1)
    answer = _ask(f"Use port {alt} instead?", str(alt))
    try:
        return int(answer)
    except ValueError:
        _warn(f"Invalid port number, using {alt}.")
        return alt


# ── Step: LaunchAgent ───────────────────────────────────────────────────────


def _generate_plist(python_path: str, port: int, claude_dir: str | None) -> str:
    """Generate the LaunchAgent plist XML with live-resolved paths.

    Args:
        python_path: Absolute path to the Python interpreter that runs bridge.
        port: The port the server will listen on.
        claude_dir: Directory containing the `claude` binary, or None.
    """
    # Build PATH: include claude's directory plus standard system dirs.
    path_parts = []
    if claude_dir:
        path_parts.append(claude_dir)
    # Add common locations
    for d in [
        str(HOME / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]:
        if Path(d).is_dir() and d not in path_parts:
            path_parts.append(d)
    path_str = ":".join(path_parts)

    # Escape XML special chars in paths
    log_path = str(BRIDGE_DIR / "serve.log")
    esc = _xml_escape

    return textwrap.dedent(f"""\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
        <key>Label</key>
        <string>{LAUNCHD_LABEL}</string>

        <key>ProgramArguments</key>
        <array>
            <string>{esc(python_path)}</string>
            <string>-m</string>
            <string>bridge</string>
            <string>serve</string>
        </array>

        <!-- `bridge serve` works from any directory when installed as a
             package; the home directory is a safe fallback. -->
        <key>WorkingDirectory</key>
        <string>{esc(str(HOME))}</string>

        <!-- launchd hands out a minimal PATH. Include the directories that
             contain `claude` so the panel can launch sessions. -->
        <key>EnvironmentVariables</key>
        <dict>
            <key>PATH</key>
            <string>{esc(path_str)}</string>
            <key>BRIDGE_PORT</key>
            <string>{port}</string>
        </dict>

        <!-- Start at login and keep it up. -->
        <key>RunAtLoad</key>
        <true/>

        <key>KeepAlive</key>
        <true/>

        <!-- If the port is taken, uvicorn exits; bound retries to 10s. -->
        <key>ThrottleInterval</key>
        <integer>10</integer>

        <key>StandardOutPath</key>
        <string>{esc(log_path)}</string>

        <key>StandardErrorPath</key>
        <string>{esc(log_path)}</string>
    </dict>
    </plist>""")


def _wait_until_unloaded(uid: int, timeout: float = 5.0) -> None:
    """Block until launchd no longer knows the label, or `timeout` elapses.

    `launchctl print` exits non-zero once the service is fully unloaded, which
    is the signal a following `bootstrap` needs. Returning after the timeout
    rather than raising keeps this best-effort: a stuck bootout should not stop
    us from attempting the bootstrap and surfacing its own error.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
            check=False, capture_output=True, text=True,
        )
        if probe.returncode != 0:
            return
        time.sleep(0.1)


def _install_launchd(plist_path: str) -> bool:
    """Copy the plist to ~/Library/LaunchAgents and bootstrap it.

    Returns True on success.
    """
    dest = LAUNCHD_AGENTS_DIR / LAUNCHD_PLIST_NAME
    try:
        LAUNCHD_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plist_path, dest)
    except OSError as exc:
        _warn(f"Could not copy plist to {dest}: {exc}")
        return False

    uid = os.getuid()
    # Boot out any already-loaded instance first. `bootstrap` fails with EIO
    # ("Input/output error", error 5) against a label that is already loaded,
    # which is the normal state for `--launchd-only` (regenerating the plist of
    # an agent that is already running). Best-effort: on a fresh install nothing
    # is loaded and this returns non-zero, which is fine and ignored.
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
        check=False, capture_output=True, text=True,
    )
    # `bootout` is asynchronous: it returns before the job has finished exiting,
    # and an immediate `bootstrap` races the still-dying service and hits the
    # same EIO. Wait until the label is actually gone before bootstrapping.
    _wait_until_unloaded(uid)
    # Bootstrap
    try:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(dest)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        _warn(f"launchctl bootstrap failed: {exc.stderr.strip()}")
        _info(f"The plist was copied to {dest}.")
        _info(f"To start manually: launchctl bootstrap gui/$(id -u) {dest}")
        return False

    return True


def _uninstall_launchd() -> bool:
    """Bootout and remove the LaunchAgent. Returns True if anything was done."""
    dest = LAUNCHD_AGENTS_DIR / LAUNCHD_PLIST_NAME
    if not dest.exists():
        _info("No LaunchAgent installed — nothing to remove.")
        return False

    # Bootout
    try:
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
            check=False, capture_output=True, text=True,
        )
    except Exception:
        pass  # Already not loaded

    try:
        dest.unlink()
        _ok(f"Removed {dest}")
    except OSError as exc:
        _warn(f"Could not remove {dest}: {exc}")
        return False

    return True


def _step_launchd(python_path: str, port: int) -> bool:
    """Offer to install the LaunchAgent. Returns True if installed."""
    _banner("LaunchAgent")
    print(textwrap.dedent("""\
      Bridge runs as a background agent (not a daemon): it reads ~/.claude
      and spawns Terminal windows, so it needs the logged-in GUI session.

      Installing the LaunchAgent means:
        - Bridge starts automatically when you log in
        - It restarts if it crashes
        - Claude Code hooks (Notification, SessionStart, SessionEnd)
          can reach it at any time

      Without the LaunchAgent, you need to run `bridge serve` manually
      each time you want the panel."""))
    print()

    if not _ask_yn("Install the LaunchAgent?", default=True):
        _info("Skipping LaunchAgent. Run `bridge serve` manually when you want the panel.")
        return False

    claude_dir = _resolve_claude_path()
    if claude_dir is None:
        _warn("`claude` not found on PATH — session launching may fail.")
        _info("Make sure `claude` is on PATH before launching sessions from Bridge.")

    plist_xml = _generate_plist(python_path, port, claude_dir)
    try:
        BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
        LAUNCHD_PLIST_PATH.write_text(plist_xml, encoding="utf-8")
    except OSError as exc:
        _warn(f"Could not write plist: {exc}")
        return False

    if _install_launchd(str(LAUNCHD_PLIST_PATH)):
        _ok("LaunchAgent installed and started.")
        return True
    return False


# ── Step: handoff command ───────────────────────────────────────────────────


def _step_handoff() -> bool:
    """Offer to copy the /handoff slash command. Returns True if installed."""
    _banner("Handoff command")
    print(textwrap.dedent("""\
      `/handoff` is a Claude Code slash command that captures a summary
      and next-session prompt at the end of a session. Bridge reads it
      and shows it on the project card so the next session starts
      exactly where this one left off."""))
    print()

    if not _ask_yn("Install the /handoff slash command?", default=True):
        _info("Skipping. You can install it later by running `bridge setup` again.")
        return False

    # Read the bundled handoff.md
    try:
        from importlib.resources import files as resource_files

        src = resource_files("bridge") / HANDOFF_SRC
        content = src.read_text(encoding="utf-8")
    except Exception:
        # Fallback: try relative to this file (for editable installs)
        try:
            src = Path(__file__).resolve().parent.parent.parent / "commands" / "handoff.md"
            content = src.read_text(encoding="utf-8")
        except Exception:
            _warn("Could not find the handoff command file. Bridge may not be fully installed.")
            _info("The file should be at commands/handoff.md in the Bridge repository.")
            return False

    # Write to ~/.claude/commands/
    try:
        CLAUDE_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
        if HANDOFF_DEST.exists():
            if not _ask_yn(f"{HANDOFF_DEST} already exists. Overwrite?", default=False):
                _info(f"Leaving existing {HANDOFF_DEST} unchanged.")
                return True
        HANDOFF_DEST.write_text(content, encoding="utf-8")
        _ok(f"Installed /handoff: {HANDOFF_DEST}")
        return True
    except OSError as exc:
        _warn(f"Could not write {HANDOFF_DEST}: {exc}")
        return False


# ── Step: config file ───────────────────────────────────────────────────────


def _generate_config(discovery_paths: list[str], port: int) -> str:
    """Generate a config.toml with the user's choices."""
    lines = [
        "# Bridge configuration — generated by `bridge setup`.",
        "# Edit this file to add aliases, adjust stale detection, or change discovery paths.",
        "",
    ]

    # `port` is a top-level key, so it must precede every `[table]` header: a
    # bare key written after `[discovery]` would be parsed as `discovery.port`.
    # Stored here so `bridge setup --launchd-only` can recover it without the
    # user having to remember it.
    lines.append("# Port the panel listens on.")
    lines.append(f"port = {port}")
    lines.append("")

    if discovery_paths:
        lines.append("[discovery]")
        quoted = ", ".join(f'"{_toml_str(p)}"' for p in discovery_paths)
        lines.append(f"paths = [{quoted}]")
        lines.append("")

    lines.extend([
        "# ── Aliases ─────────────────────────────────────────────────────────────",
        "# Map old project paths to current ones so a project moved or renamed",
        "# still shows as a single card.",
        "#",
        "# [aliases]",
        '# "/Users/you/old-path" = "/Users/you/new-path"',
        "",
        "# ── Archived ────────────────────────────────────────────────────────────",
        "# Paths to archive on first indexing (hide from the main list).",
        "#",
        "# [archived]",
        "# paths = []",
        "",
        "# ── Stale detection ─────────────────────────────────────────────────────",
        "# Hours after which an uncommitted change becomes 'stale'.",
        "#",
        "# [stale]",
        "# hours = 12",
        "",
    ])

    return "\n".join(lines)


def _step_config(discovery_paths: list[str], port: int) -> bool:
    """Write the config file. Returns True on success."""
    _banner("Config")
    try:
        BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warn(f"Could not create {BRIDGE_DIR}: {exc}")
        return False

    if CONFIG_PATH.exists():
        if not _ask_yn(f"{CONFIG_PATH} already exists. Overwrite?", default=False):
            _info(f"Leaving existing {CONFIG_PATH} unchanged.")
            _info("You may want to add [discovery] paths manually.")
            return True

    content = _generate_config(discovery_paths, port)
    try:
        CONFIG_PATH.write_text(content, encoding="utf-8")
        _ok(f"Wrote {CONFIG_PATH}")
        return True
    except OSError as exc:
        _warn(f"Could not write {CONFIG_PATH}: {exc}")
        return False


# ── Entry points ─────────────────────────────────────────────────────────────


def run_setup() -> int:
    """Interactive one-shot setup. Returns exit code."""
    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║        Bridge — Claude Code Panel        ║")
    print("  ╚══════════════════════════════════════════╝")
    print()
    print("  This will set up Bridge for your machine. It will:")
    print("    • Find your project directories")
    print("    • Pick a port for the web panel")
    print("    • Optionally install a background agent")
    print("    • Optionally install the /handoff slash command")
    print()
    print("  Press Ctrl-C at any time to cancel.")
    print()

    # 1. Discover project roots
    discovery_paths = _step_discovery()

    # 2. Choose port
    port = _step_port()

    # 3. Write config
    _step_config(discovery_paths, port)

    # 4. LaunchAgent (generates plist with live paths)
    python_path = sys.executable
    installed_launchd = _step_launchd(python_path, port)

    # 5. Handoff command
    _step_handoff()

    # Summary
    _banner("Done!")
    print()
    print(f"  Config:      {CONFIG_PATH}")
    print(f"  Panel:       http://127.0.0.1:{port}")
    if installed_launchd:
        print(f"  Agent:       running ({LAUNCHD_PLIST_PATH})")
    else:
        print(f"  Agent:       not installed (run `bridge serve` manually)")
    print()
    print("  Next steps:")
    print(f"    1. Run `bridge index` to scan your Claude Code transcripts")
    print(f"    2. Open http://127.0.0.1:{port}")
    print()
    print("  To uninstall later: bridge setup --uninstall")
    print()

    return 0


def run_launchd_only() -> int:
    """Regenerate and reinstall just the LaunchAgent plist."""
    # Read port from config.toml (written by `bridge setup`), falling back
    # to the env var or the default.
    from bridge.config import config_path as _config_path

    cfg_path = _config_path()
    port = DEFAULT_PORT
    if cfg_path.exists():
        try:
            import tomllib

            data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data.get("port"), int):
                port = data["port"]
        except Exception:
            pass
    port = int(os.environ.get("BRIDGE_PORT", str(port)))
    python_path = sys.executable
    claude_dir = _resolve_claude_path()

    _banner("Regenerating LaunchAgent plist")
    plist_xml = _generate_plist(python_path, port, claude_dir)
    try:
        BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
        LAUNCHD_PLIST_PATH.write_text(plist_xml, encoding="utf-8")
    except OSError as exc:
        _warn(f"Could not write plist: {exc}")
        return 1

    _ok(f"Wrote {LAUNCHD_PLIST_PATH}")

    if _ask_yn("Reinstall the LaunchAgent now?", default=True):
        if _install_launchd(str(LAUNCHD_PLIST_PATH)):
            _ok("LaunchAgent reinstalled and running.")
        else:
            return 1
    return 0


def run_uninstall() -> int:
    """Remove the LaunchAgent and optionally ~/.bridge/."""
    print()
    print("  Uninstalling Bridge...")
    print()

    _uninstall_launchd()

    if BRIDGE_DIR.exists():
        if _ask_yn(f"Remove {BRIDGE_DIR}/ and all Bridge data?", default=False):
            try:
                shutil.rmtree(BRIDGE_DIR)
                _ok(f"Removed {BRIDGE_DIR}/")
            except OSError as exc:
                _warn(f"Could not fully remove {BRIDGE_DIR}: {exc}")
        else:
            _info(f"Keeping {BRIDGE_DIR}/ — your data is preserved.")
    else:
        _info(f"{BRIDGE_DIR}/ does not exist — nothing to clean up.")

    # Check for handoff command
    if HANDOFF_DEST.exists():
        if _ask_yn(f"Remove {HANDOFF_DEST}?", default=True):
            try:
                HANDOFF_DEST.unlink()
                _ok(f"Removed {HANDOFF_DEST}")
            except OSError as exc:
                _warn(f"Could not remove {HANDOFF_DEST}: {exc}")

    print()
    print("  Bridge has been uninstalled.")
    print("  To reinstall: bridge setup")
    print()
    return 0
