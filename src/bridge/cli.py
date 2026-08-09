"""The `bridge` CLI: the only Bridge surface a Claude session touches.

It speaks HTTP to the panel and never opens the database. Two properties matter
more than anything else here:

**`bridge handoff` exits zero on every server-failure mode.** A session must
never fail because the panel is down. Under the manual-`bridge serve` uptime
model the panel is usually down, so spooling is the normal path and not an edge
case. The only non-zero exits are local usage errors detected *before* any
network attempt, where nothing can have been lost because nothing was captured.

**`bridge next` prints the prompt and nothing else**, so `claude "$(bridge next)"`
works. No banner, no log line, not even a trailing newline — anything else ends
up inside the prompt the next session receives.

**`bridge launch` is the deliberate opposite of `bridge handoff`: it exits 1 on
every server-failure mode and never spools.** That reads like an inconsistency
until you see why. A handoff that cannot reach the panel has *captured* something
the session is about to throw away, so it must be kept at any cost. A launch that
cannot reach the panel has lost nothing — the user can run `claude` themselves —
and a spooled launch would fire whenever the panel next boots, which is worse
than never firing: a session appearing hours later, in some other context, on a
prompt that has since been done by hand. So a launch either happens now or fails
loudly.

This uses `urllib` from the stdlib rather than `httpx`. The plan allowed one
dependency; none is needed. It keeps the end-of-session path free of import cost
and means the command cannot fail because a virtualenv is missing a package.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from bridge import __version__, configure_logging, spool
from bridge.config import ConfigError, load
from bridge.models import Handoff

TIMEOUT = 2.0


def _base(cfg) -> str:
    return f"http://127.0.0.1:{cfg.port}"


def _request(method: str, url: str, payload=None, timeout: float = TIMEOUT):
    """Return `(status, body)` for any HTTP reply.

    Only connection-level failures raise, so a 5xx and a refused connection are
    handled by the same branch in the caller without one masquerading as the
    other.
    """
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, (json.loads(raw) if raw else None)
        except ValueError:
            return exc.code, None


def _read_prompt(prompt_file: str) -> str:
    """`-` means stdin. The prompt is never interpolated into argv or a shell
    string: it contains quotes, backticks, newlines and `$`."""
    if prompt_file == "-":
        return sys.stdin.read()
    return Path(prompt_file).read_text(encoding="utf-8")


def cmd_handoff(args, cfg) -> int:
    try:
        prompt = _read_prompt(args.prompt_file)
    except OSError as exc:
        print(f"bridge handoff: cannot read prompt: {exc}", file=sys.stderr)
        return 2
    if not prompt.strip():
        # A local usage error, not a server failure. Exiting zero here would
        # report success for a handoff that recorded nothing.
        print("bridge handoff: refusing to record an empty prompt", file=sys.stderr)
        return 2

    h = Handoff(
        id=str(uuid.uuid4()),
        project_path=args.project or os.getcwd(),
        next_prompt=prompt,
        source_session_id=args.session_id,
        summary=args.summary,
        suggested_model=args.model,
        suggested_effort=args.effort,
        created_at=int(time.time()),
    )

    reason = None
    try:
        status, _ = _request(
            "POST", f"{_base(cfg)}/api/handoff",
            {
                "id": h.id, "project_path": h.project_path,
                "next_prompt": h.next_prompt, "session_id": h.source_session_id,
                "summary": h.summary, "suggested_model": h.suggested_model,
                "suggested_effort": h.suggested_effort, "created_at": h.created_at,
            },
        )
        if 200 <= status < 300:
            print(f"bridge: handoff {h.id} queued for {h.project_path}",
                  file=sys.stderr)
            return 0
        # Any non-2xx spools too. A 4xx means this CLI and that server disagree,
        # which is not the session's problem and must not cost it the prompt.
        reason = f"server returned {status}"
    except Exception as exc:  # noqa: BLE001 - refused, timed out, DNS, anything
        reason = f"{type(exc).__name__}: {exc}"

    try:
        path = spool.write(h, cfg.spool_dir)
    except Exception as exc:  # noqa: BLE001
        # Nowhere left to put it, so put it where the session can still see it:
        # stderr lands in the transcript. Still exit zero.
        print(f"bridge: panel unreachable ({reason}) AND spooling failed ({exc}).\n"
              f"bridge: the prompt follows so it is not lost:\n{prompt}",
              file=sys.stderr)
        return 0
    print(f"bridge: panel unreachable ({reason}); spooled to {path}", file=sys.stderr)
    return 0


def cmd_next(args, cfg) -> int:
    project = args.project or os.getcwd()
    query = urllib.parse.urlencode({"project_path": project})
    try:
        status, body = _request("GET", f"{_base(cfg)}/api/handoff?{query}")
    except Exception as exc:  # noqa: BLE001
        print(f"bridge next: panel unreachable ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 1
    if status == 204 or not body:
        # Non-zero with empty stdout, so `claude "$(bridge next)"` cannot
        # silently open a session on an empty prompt.
        print(f"bridge next: nothing queued for {project}", file=sys.stderr)
        return 1
    # Exactly the prompt. No newline is added: a trailing byte here becomes a
    # trailing byte in the next session's prompt.
    sys.stdout.write(body["next_prompt"])
    return 0


def _detail(body) -> str:
    """The server's own words, when it gave any. FastAPI's `detail` is a string
    for an `HTTPException` and a list for a 422, so only a string is quoted."""
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return f": {body['detail']}"
    return ""


def cmd_launch(args, cfg) -> int:
    """Ask the panel to spawn a session. This module never spawns one itself and
    never imports `bridge.launcher`: the server is the only process that writes
    the `launches` row, so it must also be the one that spawns."""
    # `--dangerously-skip-permissions` is spelled the way `claude` spells it so
    # muscle memory transfers, and resolves to the one enum value it means. The
    # explicit flag wins over `--permission-mode` only by being the more
    # emphatic of the two; asking for both and disagreeing is a user error worth
    # naming rather than silently resolving.
    permission_mode = args.permission_mode
    if args.dangerously_skip_permissions:
        if permission_mode and permission_mode != "bypassPermissions":
            print(
                "bridge launch: --dangerously-skip-permissions contradicts "
                f"--permission-mode {permission_mode}; pick one",
                file=sys.stderr,
            )
            return 2
        permission_mode = "bypassPermissions"

    if args.prompt_file and args.handoff:
        print(
            "bridge launch: --prompt-file contradicts --handoff; pick one",
            file=sys.stderr,
        )
        return 2

    project = args.project or os.getcwd()
    payload = {
        "project_path": project,
        "mode": args.mode,
        "model": args.model,
        "effort": args.effort,
        "permission_mode": permission_mode,
    }
    if args.prompt_file:
        try:
            prompt = _read_prompt(args.prompt_file)
        except OSError as exc:
            print(f"bridge launch: cannot read prompt: {exc}", file=sys.stderr)
            return 2
        if not prompt.strip():
            print("bridge launch: refusing to launch an empty prompt",
                  file=sys.stderr)
            return 2
        payload["prompt"] = prompt
    elif args.handoff:
        # An explicit target: no prompt is read, so this launches exactly the
        # queued handoff the caller named rather than whatever the server
        # would otherwise have to guess.
        payload["handoff_id"] = args.handoff
    else:
        # The server no longer auto-picks (Task 3 removed that fallback), so
        # the one auto-pick left lives here, and only for the unambiguous case.
        query = urllib.parse.urlencode({"project_path": project})
        try:
            status, body = _request("GET", f"{_base(cfg)}/api/handoffs?{query}")
        except Exception as exc:  # noqa: BLE001
            print(f"bridge launch: panel unreachable "
                  f"({type(exc).__name__}: {exc}); nothing was launched",
                  file=sys.stderr)
            return 1
        if not 200 <= status < 300:
            print(f"bridge launch: server returned {status}{_detail(body)}",
                  file=sys.stderr)
            return 1
        handoffs = body or []
        if len(handoffs) == 0:
            print(f"bridge launch: nothing queued for {project}; pass a prompt "
                  f"(--prompt-file) or a handoff (--handoff)", file=sys.stderr)
            return 2
        if len(handoffs) > 1:
            print("bridge launch: multiple handoffs queued; choose one with "
                  "--handoff <id>:", file=sys.stderr)
            for h in handoffs:
                print(f"  {h['id']}: {h['summary']}", file=sys.stderr)
            return 2
        payload["handoff_id"] = handoffs[0]["id"]
    # Otherwise no `prompt` key at all, rather than an empty one: the server holds
    # the queued handoff already, so round-tripping it through the client would
    # only add a way for the two to disagree about what got launched.

    try:
        status, body = _request("POST", f"{_base(cfg)}/api/launch", payload)
    except Exception as exc:  # noqa: BLE001 - refused, timed out, DNS, anything
        print(f"bridge launch: panel unreachable "
              f"({type(exc).__name__}: {exc}); nothing was launched",
              file=sys.stderr)
        return 1
    if not 200 <= status < 300:
        print(f"bridge launch: server returned {status}{_detail(body)}",
              file=sys.stderr)
        return 1

    result = body or {}
    if result.get("outcome") != "started":
        # A launch failure is a 200 with `outcome='failed'` so the panel's UI can
        # show the error next to the prompt. Here it is still a failed launch.
        print(f"bridge launch: {result.get('error') or 'nothing was launched'}",
              file=sys.stderr)
        return 1
    # stderr, like `handoff`: nothing should ever parse this command's stdout.
    print(f"bridge: launched {args.mode} session "
          f"{result.get('session_id') or result.get('launch_id')}",
          file=sys.stderr)
    return 0


def cmd_status(args, cfg) -> int:
    project = args.project or os.getcwd()
    # The spool count is local, so status still says something useful offline.
    pending = spool.pending_count(cfg.spool_dir)
    query = urllib.parse.urlencode({"project_path": project})
    try:
        status, body = _request("GET", f"{_base(cfg)}/api/handoff?{query}")
        panel = "up"
    except Exception:  # noqa: BLE001
        status, body, panel = None, None, "down"

    from bridge import update
    _sha = update.installed_sha()
    print(f"version: {__version__}")
    print(f"build:   {(_sha[:12] if _sha else 'dev')} ({update.install_method()})")
    print(f"project: {project}")
    print(f"panel:   {panel} ({_base(cfg)})")
    print(f"spooled: {pending} awaiting drain")
    if panel == "up":
        if status == 204 or not body:
            print("queued:  nothing")
        else:
            summary = body.get("summary") or "(no summary)"
            print(f"queued:  {summary}")
    return 0


def cmd_open(args, cfg) -> int:
    url = _base(cfg)
    try:
        subprocess.run(["/usr/bin/open", url], check=False)
    except OSError as exc:
        print(f"bridge open: {exc}; the panel is at {url}", file=sys.stderr)
        return 1
    return 0


def cmd_update(args, cfg) -> int:
    """Resolve the SHA the check surfaced (or the remote HEAD) and install it.

    Prints to stderr like `handoff`/`launch`: nothing parses this stdout."""
    from bridge import update

    installed = update.installed_sha()
    # The one-shot LaunchAgent updater invokes `bridge update --sha <sha>` with
    # the exact commit already resolved: honor it directly and skip both the
    # panel query and the network resolve, but still refuse a no-op below.
    target = getattr(args, "sha", None)
    # Prefer the SHA the panel already surfaced so the CLI and button install the
    # SAME commit; fall back to a fresh resolve when the panel is down.
    if target is None:
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


def cmd_diagnose(args, cfg) -> int:
    """A read-only snapshot for a bug report: versions, resolved config, the
    LaunchAgent's state and the tail of the serve log. Imported lazily so the
    handoff path never pays for `platform`/`subprocess` it does not use."""
    from bridge import diagnose

    print(diagnose.render(cfg, log_lines=args.lines))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge")
    # `--version` carries the build SHA and install method so a bug report
    # names the exact commit. Imported lazily here (not at module top) so the
    # handoff/next fast path never pays for `bridge.update`'s imports;
    # argparse needs the finished version string at parse-build time, so it
    # cannot wait until an action callback.
    from bridge import update as _u
    _sha = _u.installed_sha()
    _short = _sha[:12] if _sha else "dev"
    parser.add_argument("--version", action="version",
                        version=f"bridge {__version__} ({_short} {_u.install_method()})")
    sub = parser.add_subparsers(dest="cmd")

    h = sub.add_parser("handoff", help="record a next-session prompt")
    h.add_argument("--summary")
    h.add_argument("--prompt-file", required=True,
                   help="path to the prompt, or - for stdin")
    h.add_argument("--project")
    h.add_argument("--session-id")
    h.add_argument("--model")
    h.add_argument("--effort")

    la = sub.add_parser("launch", help="launch the queued prompt as a session")
    la.add_argument("--project")
    la.add_argument("--mode", choices=("terminal", "background"),
                    default="terminal")
    la.add_argument("--model")
    la.add_argument("--effort")
    # Choices restated rather than imported: this module never imports
    # `bridge.launcher` (the server is the only process that spawns), and the
    # server revalidates against `launcher.PERMISSION_MODES` regardless, so the
    # duplication buys a local `--help` without weakening the real gate.
    la.add_argument("--permission-mode",
                    choices=("acceptEdits", "auto", "bypassPermissions",
                             "manual", "dontAsk", "plan"))
    la.add_argument("--dangerously-skip-permissions", action="store_true",
                    help="alias for --permission-mode bypassPermissions")
    la.add_argument("--prompt-file",
                    help="path to a prompt, or - for stdin; optional -- with "
                         "neither this nor --handoff, launches the project's "
                         "sole queued handoff, or lists choices if several")
    la.add_argument("--handoff",
                    help="launch this queued handoff by id")

    n = sub.add_parser("next", help="print the queued prompt to stdout")
    n.add_argument("--project")

    s = sub.add_parser("status", help="show panel and handoff state")
    s.add_argument("--project")

    up = sub.add_parser("update", help="update Bridge to the latest main HEAD")
    up.add_argument("--project")
    up.add_argument("--sha", help="install this exact commit (used by the "
                                  "one-shot LaunchAgent updater)")

    d = sub.add_parser("diagnose",
                       help="print a read-only diagnostics snapshot")
    d.add_argument("--lines", type=int, default=40,
                   help="how many trailing lines of serve.log to show")

    sub.add_parser("open", help="open the panel in a browser")

    su = sub.add_parser("setup", help="interactive first-time setup")
    su.add_argument("--launchd-only", action="store_true",
                    help="regenerate and reinstall only the LaunchAgent plist")
    su.add_argument("--uninstall", action="store_true",
                    help="remove the LaunchAgent and optionally ~/.bridge/")

    # Accepted here so `bridge index` and `bridge serve` work, but handled by
    # bridge.__main__ and imported lazily: those open the database, and this
    # module must not.
    lazy_help = {
        "index": "scan Claude Code transcripts into the database",
        "serve": "start the panel (blocks; Ctrl-C to stop)",
        "backfill": "import stray HANDOFF.md / NEXT-SESSION.md files",
    }
    for name in ("index", "serve", "backfill"):
        p = sub.add_parser(name, help=lazy_help[name])
        p.add_argument("--projects-dir")
        p.add_argument("--db")
        p.add_argument("--spool-dir")
        if name == "backfill":
            p.add_argument("--write", action="store_true")
            p.add_argument("--dry-run", action="store_true")
    return parser


HANDLERS = {
    "handoff": cmd_handoff,
    "launch": cmd_launch,
    "next": cmd_next,
    "status": cmd_status,
    "update": cmd_update,
    "diagnose": cmd_diagnose,
    "open": cmd_open,
}


def _run_setup(args) -> int:
    from bridge.setup import run_launchd_only, run_setup, run_uninstall

    if args.uninstall:
        return run_uninstall()
    if args.launchd_only:
        return run_launchd_only()
    return run_setup()


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.cmd in ("index", "serve", "backfill"):
        # Lazy: importing this pulls in sqlite3 and the store, and the handoff
        # path must stay free of both.
        from bridge.__main__ import run_db_command

        return run_db_command(argv)

    handler = HANDLERS.get(args.cmd)
    if handler is None:
        if args.cmd == "setup":
            return _run_setup(args)
        parser.print_usage(sys.stderr)
        return 2
    # `ConfigError` already says exactly what is wrong (which key, which file,
    # what was found). Letting it propagate turned `BRIDGE_PORT=oops bridge
    # status` -- a plain typo, and the first thing a new user does after reading
    # the env-var table -- into a twelve-frame traceback that reads like a crash
    # in Bridge rather than a mistake in the shell.
    try:
        cfg = load()
    except ConfigError as exc:
        print(f"bridge: {exc}", file=sys.stderr)
        return 2
    return handler(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
