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

from bridge import spool
from bridge.config import load
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge")
    sub = parser.add_subparsers(dest="cmd")

    h = sub.add_parser("handoff", help="record a next-session prompt")
    h.add_argument("--summary")
    h.add_argument("--prompt-file", required=True,
                   help="path to the prompt, or - for stdin")
    h.add_argument("--project")
    h.add_argument("--session-id")
    h.add_argument("--model")
    h.add_argument("--effort")

    n = sub.add_parser("next", help="print the queued prompt to stdout")
    n.add_argument("--project")

    s = sub.add_parser("status", help="show panel and handoff state")
    s.add_argument("--project")

    sub.add_parser("open", help="open the panel in a browser")

    # Accepted here so `bridge index` and `bridge serve` work, but handled by
    # bridge.__main__ and imported lazily: those open the database, and this
    # module must not.
    for name in ("index", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--projects-dir")
        p.add_argument("--db")
        p.add_argument("--spool-dir")
    return parser


HANDLERS = {
    "handoff": cmd_handoff,
    "next": cmd_next,
    "status": cmd_status,
    "open": cmd_open,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.cmd in ("index", "serve"):
        # Lazy: importing this pulls in sqlite3 and the store, and the handoff
        # path must stay free of both.
        from bridge.__main__ import run_db_command

        return run_db_command(argv)

    handler = HANDLERS.get(args.cmd)
    if handler is None:
        parser.print_usage(sys.stderr)
        return 2
    return handler(args, load())


if __name__ == "__main__":
    raise SystemExit(main())
