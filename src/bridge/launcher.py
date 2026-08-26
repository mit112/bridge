"""Launch command construction, and the one function that spawns.

The module splits in two and the split is the design. Everything above
`# --- spawning ---` is a pure function from arguments to a string: no file, no
process, no database. Every escaping decision lives there, in functions whose
whole contract is `arguments in, string out`, which is what makes the hostile
cases exhaustively testable. `launch()` below is the only impure thing here, and
it takes its process runner by injection so no test ever opens a real Terminal
window or starts a real Claude session.

Measured against the real environment before any of this was written:
  * A terminal launch nests a shell command inside an AppleScript literal inside
    `osascript -e`, and `do script` runs an INTERACTIVE zsh, so history
    expansion is live and a `zsh -c` test would not catch it. The naive inline
    form is a full RCE:
      sent      claude … "INLINE $(echo PWNED) `echo BACKTICK_EXECUTED` ${HOME}"
      received  b'INLINE PWNED BACKTICK_EXECUTED /Users/you'
    So the prompt never crosses either layer: it is written to a file (that is
    `launch`'s job, not this module's) and the command reads it back with
    `"$(/bin/cat '<file>')"`. That round-tripped a 483-byte hostile fixture
    byte-exact from a project directory named `proj dir's "na\\me"`.
  * Command-substitution output inside double quotes is not re-scanned for
    metacharacters. Confirmed empirically, not assumed.
  * `claude --bg` IGNORES `--session-id` and prints "warning: --bg manages the
    session id; ignoring --session-id", so `build_bg_argv` omits the flag. The
    two modes genuinely correlate differently; see the plan's Self-Review §4.
  * There is no flag that reads the USER prompt from a file.
    `--system-prompt-file` sets the system prompt. Hence `$(cat …)`.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from bridge import spool
from bridge.models import Launch
from bridge.registry import resolve_project
from bridge.store import now_epoch

CAT = "/bin/cat"
OSASCRIPT = "/usr/bin/osascript"

# `mode` values, shared with `launches.mode` and `POST /api/launch`.
MODES = ("terminal", "background")

# Measured, not guessed: ARG_MAX on this machine is 1,048,576; 900 KiB launched
# fine and 1024 KiB failed with "argument list too long" (rc 127). The prompt
# file does NOT lift ARG_MAX -- `$(cat …)` still becomes argv -- so the cap
# applies to both modes and is set well below the wall.
MAX_PROMPT_BYTES = 800 * 1024

# `claude` validates 8-4-4-4-12 hex case-insensitively. Matching it here is what
# makes it safe to emit `--session-id` unquoted.
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

TITLE_MAX = 60

# The exact set `claude --permission-mode` accepts, measured against 2.1.220.
# Unlike `model` and `effort` -- capability knobs passed through unvalidated
# because the CLI is the authority -- this is a SAFETY control, so it is
# validated against a closed set and fails closed. An unrecognised value must
# never become a flag: the failure mode is a session running with permissions
# nobody chose. A future CLI mode therefore needs adding here deliberately,
# which is the intended cost.
PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
)


class LaunchError(Exception):
    """A launch that cannot be constructed safely, so it is not constructed."""


@dataclass(frozen=True)
class LaunchSpec:
    """Everything a launch needs that is not a side effect.

    `session_id` is meaningful in **terminal mode only**: it is pre-assigned so
    the indexer can join the launch to `<uuid>.jsonl` on its next scan.
    Background mode has none at spawn time, because `--bg` mints its own.
    """

    project_path: str
    prompt: str
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    title: str | None = None
    mode: str = "terminal"
    # None or "" means emit no `--permission-mode` at all. Never sticky and
    # never armed from a handoff: a persisted default would silently apply a
    # dangerous mode to a launch nobody was watching.
    permission_mode: str | None = None


def new_session_id() -> str:
    """A pre-assignable session id, lowercase.

    Lowercase is load-bearing. `claude`'s format gate is case-insensitive and
    does not normalise, while APFS is case-insensitive: an uppercase id passes
    validation, then collides with a lowercase transcript, while the id we
    recorded and the filename on disk disagree. `uuid4` is already lowercase;
    the call says so explicitly so nobody "tidies" it into `.upper()`.
    """
    return str(uuid.uuid4()).lower()


def validate_prompt(prompt: str) -> None:
    """Raise `LaunchError` if `prompt` cannot survive the trip to argv.

    Called before anything is constructed, and before Task 3 writes the prompt
    file, so a rejected launch has no side effects at all.
    """
    nul = prompt.find("\x00")
    if nul != -1:
        raise LaunchError(
            f"prompt contains NUL at byte {nul}: NUL truncates argv "
            "(`before\\x00after` arrives as b'before'), so it is refused here"
        )
    size = len(prompt.encode("utf-8"))
    if size > MAX_PROMPT_BYTES:
        raise LaunchError(
            f"prompt is {size} bytes, over the {MAX_PROMPT_BYTES}-byte limit"
        )


def sh_quote(s: str) -> str:
    """POSIX single-quote quoting: `'` becomes `'\\''`.

    Not `shlex.quote`, which leaves a "safe" string bare. Unconditional quoting
    keeps the emitted command one fixed shape regardless of the value, and
    `do script` runs an interactive zsh where an unquoted `!!` in a title is
    RCE-adjacent -- so "safe enough to leave bare" is not a judgement worth
    delegating here.
    """
    return "'" + s.replace("'", "'\\''") + "'"


def as_quote(s: str) -> str:
    """Quote `s` as an AppleScript string literal.

    Backslash is escaped BEFORE double quote. The other order escapes the
    backslash it just introduced and is the classic way to get this wrong.
    `$`, backtick and `!` are not special to AppleScript and are deliberately
    left alone; the shell layer's single quotes are what neutralise them.
    """
    if "\n" in s or "\r" in s:
        # An AppleScript literal cannot hold a line break, and a newline in a
        # project path is legal in APFS. Raise rather than silently corrupt.
        raise LaunchError(
            "an AppleScript literal cannot contain a newline or carriage return"
        )
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def resolve_claude(which: Callable[[str], str | None] = shutil.which) -> str:
    """The `claude` executable, from `PATH`, never a hardcoded path.

    `gitprobe` hardcodes `GIT = "/usr/bin/git"`, which is right there and would
    be fatal here: with a hardcoded path every fake-`claude`-on-`PATH` test
    passes vacuously while testing nothing. The injected `which` is what keeps
    those tests honest.
    """
    found = which("claude")
    if found is None:
        raise LaunchError("claude is not on PATH; cannot launch a session")
    return found


def sanitize_title(s: str, limit: int = TITLE_MAX) -> str:
    """First line, control characters removed, truncated.

    `claude`'s `-n/--name` has no length or character validation and is injected
    straight into the terminal-title escape sequence, so a `\\x1b` or `\\x07` in
    a title rewrites the terminal. Nothing downstream will strip them.
    """
    first = s.replace("\r", "\n").split("\n", 1)[0]
    kept = "".join(c for c in first if c.isprintable())
    return kept.strip()[:limit]


def default_title(summary: str | None, project_name: str) -> str:
    """The handoff summary's first line, falling back to the project name."""
    return sanitize_title(summary or "") or sanitize_title(project_name)


def permission_flag(spec: LaunchSpec) -> bool:
    """Whether to emit `--permission-mode`, validating the value first.

    Returns False for None and "" -- the select's default option posts an empty
    string, and `--permission-mode ''` is not the same as omitting the flag.
    Raises rather than dropping an unrecognised value: silently ignoring it
    would launch under whatever the CLI's own default is while the panel showed
    something else.
    """
    mode = spec.permission_mode
    if not mode:
        return False
    if mode not in PERMISSION_MODES:
        raise LaunchError(
            f"permission mode {mode!r} is not one of "
            f"{', '.join(sorted(PERMISSION_MODES))}"
        )
    return True


def build_shell_command(
    spec: LaunchSpec, prompt_path: str | Path, claude: str | None = None
) -> str:
    """The shell command a Terminal window runs. The prompt is NOT in it.

    The `[ -r … ]` guard is load-bearing, not defensive noise: without it a
    missing or empty prompt file makes `cat` write to stderr, leaves the shell's
    exit status at 0, and launches `claude` with an EMPTY prompt -- observed as
    `argc=4` with a final `b''`. A silent empty session is the worst failure
    available here because it looks like it worked.

    The double quotes around `$(…)` are equally load-bearing: without them word
    splitting shatters the prompt into many argv elements.
    """
    validate_prompt(spec.prompt)
    claude = claude or resolve_claude()
    file_q = sh_quote(str(prompt_path))

    argv = [sh_quote(claude)]
    if spec.session_id:
        if not SESSION_ID_RE.match(spec.session_id):
            raise LaunchError(f"session id {spec.session_id!r} is not lowercase 8-4-4-4-12 hex")
        argv += ["--session-id", spec.session_id]  # unquoted: validated above
    # Omitted ENTIRELY when unset. `--model ''` is not the same as no --model,
    # and model/effort are passed through unvalidated per the spec -- the CLI is
    # the authority on what it accepts -- which is exactly why they are quoted.
    if spec.model:
        argv += ["--model", sh_quote(spec.model)]
    if spec.effort:
        argv += ["--effort", sh_quote(spec.effort)]
    # Unquoted, like `--session-id`, and for the same reason: it is validated
    # against a closed set above, so the token emitted here is always one of six
    # fixed literals and can never be assembled from caller text.
    if permission_flag(spec):
        argv += ["--permission-mode", spec.permission_mode]
    title = sanitize_title(spec.title or "")
    if title:
        argv += ["-n", sh_quote(title)]
    argv.append(f'"$({CAT} {file_q})"')

    guard = (
        f"[ -r {file_q} ] || "
        "{ echo 'bridge: prompt file missing' >&2; exit 1; }"
    )
    return f"{guard}; cd {sh_quote(spec.project_path)} && {' '.join(argv)}"


def build_applescript(command: str) -> str:
    """AppleScript that runs `command` in a new Terminal window."""
    return (
        'tell application "Terminal"\n'
        f"\tdo script {as_quote(command)}\n"
        "\tactivate\n"
        "end tell"
    )


def build_bg_argv(spec: LaunchSpec, claude: str | None = None) -> list[str]:
    """argv for a background launch. No shell, no quoting -- argv passes bytes.

    `--session-id` is omitted deliberately and permanently. `claude --bg`
    ignores it and warns, so passing it is noise that also implies a
    correlation this code does not have. `-n/--name` is kept: it survives into
    both the `backgrounded · <short>` stdout line and `claude agents --json`,
    and is the best human-readable tie-back there is.
    """
    validate_prompt(spec.prompt)
    claude = claude or resolve_claude()

    argv = [claude, "--bg"]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.effort:
        argv += ["--effort", spec.effort]
    if permission_flag(spec):
        argv += ["--permission-mode", spec.permission_mode]
    title = sanitize_title(spec.title or "")
    if title:
        argv += ["-n", title]
    argv.append(spec.prompt)  # exactly one element, verbatim
    return argv


# --- spawning ----------------------------------------------------------------
#
# The order below is fixed and load-bearing: resolve `claude`, write the prompt
# file, insert the `launches` row as `pending`, spawn, record the outcome.
# Anything that fails before the row exists fails with no side effects; anything
# after it is correlatable, which is why a failed spawn still leaves a row.

# A pre-assigned id must be unused, and "unused" is per-project-dir: `claude`
# `statSync`s `<cwd-project-dir>/<uuid>.jsonl` and exits 1 with
# "Error: Session ID … is already in use." A collision is therefore recoverable
# by minting another id, and the bound is what stops a systematic cause (a
# read-only project dir, say) from becoming an unbounded spawn loop.
MAX_SESSION_ID_ATTEMPTS = 3
SESSION_ID_IN_USE_RE = re.compile(r"session id\b.*\bis already in use", re.I)

# CSI sequences, OSC strings, and bare two-character escapes. The `--bg` handle
# arrives colour-wrapped and whether chalk disables itself on a pipe was NOT
# confirmed, so this strips defensively rather than trusting a TTY check: an
# unstripped `\x1b[36mdeadbeef\x1b[0m` is a short id no glob or lookup can match.
ANSI_RE = re.compile(
    r"\x1b\[[0-9;:?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)

# `backgrounded · <short>[ · <name>]`, where `short` is 8 lowercase hex and is
# exactly `session_id[:8]`. The separator is matched loosely because it is a
# decoration; the handle is matched exactly because it is a correlation key.
BG_HANDLE_RE = re.compile(r"backgrounded[^0-9a-f]*([0-9a-f]{8})(?![0-9a-f])")


@dataclass(frozen=True)
class LaunchResult:
    """What the caller needs to report, and to fall back on.

    `error` is the text a failed launch shows next to the copied prompt, so it is
    a string and not an exception: a launch failure is a normal outcome the panel
    renders, not a 500. `note` carries the non-fatal ones — a background launch
    that started but printed no handle is `started` with a note, because marking
    it failed would leave its handoff queued for a session that is running.
    """

    launch_id: str
    outcome: str
    session_id: str | None = None
    short_id: str | None = None
    error: str | None = None
    note: str | None = None


def write_prompt_file(launches_dir: str | Path, session_id: str, prompt: str) -> Path:
    """Write `<session_id>.prompt` atomically, directory 0700 and file 0600.

    Takes the directory rather than a `Config` so `tests/conftest.py` can wrap it
    with the same real-`~/.bridge` guard it wraps every `spool` writer with — a
    guard that inspects arguments cannot see a path hidden inside a dataclass.

    The prompt is `rstrip("\\n")`-normalised here, and that is not cosmetic:
    `$(cat …)` strips trailing newlines while the `--bg` argv path preserves
    them, so normalising at the single point both modes share is what holds them
    to one byte-exact assertion instead of two correctness standards.
    """
    directory = Path(launches_dir)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)

    final = directory / f"{session_id}.prompt"
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{session_id}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt.rstrip("\n"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return final


def gc_prompt_files(
    launches_dir: str | Path, max_age_days: int = 14, now: float | None = None
) -> int:
    """Delete prompt files older than `max_age_days`. Returns how many went.

    Age-based, never launch-time: `do script` returns immediately and the new
    shell's `cat` runs later, so unlinking when `osascript` returns is a live
    race whose prize is an empty session that looks like it worked. The file is
    also provenance — literally what ran — so it is kept until it is stale.
    """
    directory = Path(launches_dir)
    if not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - max_age_days * 86400
    removed = 0
    for path in sorted(directory.glob("*.prompt")):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:  # raced with another GC, or not ours to delete
            continue
    return removed


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def parse_bg_handle(stdout: str) -> str | None:
    """The 8-hex handle `--bg` prints, or None if it printed something else."""
    match = BG_HANDLE_RE.search(strip_ansi(stdout))
    return match.group(1) if match else None


def resolve_short_id(short_id: str, claude: str, run) -> str | None:
    """`short` → full UUID via `claude agents --json --all`. Best effort only.

    Confirmed live: `"id": "00b31445"` sits alongside
    `"sessionId": "00b31445-a2d0-4d3b-878b-e37f81284385"`. When this cannot
    answer — the subcommand changed, the output is not JSON, the agent has not
    registered yet — the launch keeps its `short_id` alone and Task 7's unique
    prefix backfill closes the loop on the next index. Guessing here would bind
    a launch to the wrong session, which is worse than waiting.
    """
    try:
        proc = run([claude, "agents", "--json", "--all"],
                   capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        data = json.loads(strip_ansi(proc.stdout or ""))
        for entry in _iter_agent_dicts(data):
            if entry.get("id") != short_id:
                continue
            session_id = str(entry.get("sessionId") or "").lower()
            if SESSION_ID_RE.match(session_id) and session_id.startswith(short_id):
                return session_id
    except Exception:  # noqa: BLE001 - a best-effort lookup cannot fail a launch
        return None
    return None


def _iter_agent_dicts(data):
    """Yield the dicts in `agents --json` output, whatever it is wrapped in."""
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                yield entry


def launch(
    store,
    cfg,
    spec: LaunchSpec,
    handoff_id: str | None = None,
    *,
    run=None,
    which: Callable[[str], str | None] = shutil.which,
) -> LaunchResult:
    """Spawn one session and record what happened. The only impure entry point.

    `run` is injected and defaults to `subprocess.run`. That injection is not
    testability polish: terminal mode shells out to `/usr/bin/osascript`, which
    would open a real Terminal window and start a real, token-burning session
    whose transcript the indexer would then ingest. No test may ever do that, so
    every test substitutes `run`.

    Bridge launches sessions; it never hosts one. Once the spawn returns this
    function's job is over — it does not wait on, poll, or kill anything.
    """
    run = run or subprocess.run

    # Everything that can refuse a launch outright happens first, so a refusal
    # writes no file, inserts no row, and spawns nothing.
    if spec.mode not in MODES:
        raise LaunchError(f"mode {spec.mode!r} is not one of {MODES}")
    # A path that is not a directory cannot be `cd`'d into, so a terminal
    # launch opens a real window only to die on its first line, and a
    # background launch fails inside `subprocess.run`'s own `cwd` handling --
    # both after `resolve_project` has already created a registry row for a
    # project that does not exist. Refusing here keeps that row from being
    # written at all. Deliberately NOT gated on `resolve_project` returning an
    # already-indexed project: that function CREATES rows by design (see
    # `test_api.py`'s "capturing a handoff must never 404 because the project
    # is unindexed"), so gating on it would break the first launch out of any
    # fresh repo.
    if not Path(spec.project_path).is_dir():
        raise LaunchError(
            f"project path {spec.project_path!r} is not a directory; "
            "there is nowhere to start a session"
        )
    validate_prompt(spec.prompt)
    claude = resolve_claude(which)

    prompt = spec.prompt.rstrip("\n")
    project_id = resolve_project(store, spec.project_path)
    if handoff_id is not None and store.claim_queued_handoff(handoff_id, project_id) is None:
        # Existence was already checked at the API edge; this is the part
        # that check cannot do -- reject a handoff that belongs to a
        # DIFFERENT project, or one a concurrent launch already claimed,
        # before anything spawns. Nothing has been written yet (`_new_row`
        # is below this), so refusing here leaves no row and no journal
        # entry, matching every other refusal in this function.
        raise LaunchError(
            f"handoff {handoff_id!r} is not a queued handoff for this project"
        )
    if spec.mode == "background":
        return _launch_background(store, cfg, spec, prompt, project_id,
                                  handoff_id, claude, run)
    return _launch_terminal(store, cfg, spec, prompt, project_id,
                            handoff_id, claude, run)


def _new_row(store, spec, prompt, project_id, handoff_id, session_id) -> str:
    launch_id = str(uuid.uuid4())
    store.create_launch(Launch(
        id=launch_id,
        project_id=project_id,
        mode=spec.mode,
        prompt=prompt,
        handoff_id=handoff_id,
        session_id=session_id,
        model=spec.model,
        effort=spec.effort,
        launched_at=now_epoch(),
        outcome="pending",
    ))
    return launch_id


def _launch_terminal(store, cfg, spec, prompt, project_id, handoff_id, claude, run):
    session_id = spec.session_id or new_session_id()
    # The prompt file is written before the row exists, which is the one ordering
    # inversion allowed here: its name is derived from the session id, so a
    # failure to write it must not leave a row claiming a session that has no
    # prompt to run.
    prompt_path = write_prompt_file(cfg.launches_dir, session_id, prompt)
    launch_id = _new_row(store, spec, prompt, project_id, handoff_id, session_id)

    error = None
    for attempt in range(1, MAX_SESSION_ID_ATTEMPTS + 1):
        command = build_shell_command(
            replace(spec, session_id=session_id, prompt=prompt), prompt_path,
            claude=claude,
        )
        script = build_applescript(command)
        try:
            proc = run([OSASCRIPT, "-e", script], capture_output=True, text=True)
        except OSError as exc:
            error = f"could not run {OSASCRIPT}: {exc}"
            break

        if proc.returncode == 0:
            # No `set_launch_session` here: the row already holds the id it was
            # inserted with, and `short_id` is `session_id[:8]` by construction —
            # a terminal launch never needs the prefix backfill Task 7 does for
            # background, so writing it would be a second UPDATE for nothing.
            return _started(store, cfg, launch_id, handoff_id,
                            session_id=session_id, short_id=session_id[:8])

        error = _spawn_error(OSASCRIPT, proc)
        if not _is_session_id_collision(proc) or attempt == MAX_SESSION_ID_ATTEMPTS:
            break
        # Recoverable, and only this: mint a fresh id, write its prompt file, and
        # point the row at it before trying again.
        session_id = new_session_id()
        prompt_path = write_prompt_file(cfg.launches_dir, session_id, prompt)
        store.set_launch_session(launch_id, session_id, session_id[:8])

    return _failed(store, launch_id, handoff_id, error)


def _launch_background(store, cfg, spec, prompt, project_id, handoff_id, claude, run):
    """No prompt file and no shell: the prompt is one argv element.

    Background mode is deliberately not routed through the prompt file, so the
    two modes fail independently. Its row is written with `session_id` NULL
    because `--bg` discards `--session-id` and mints its own; recording the
    pre-assigned one would hold a correlation key matching no transcript that
    will ever exist.
    """
    launch_id = _new_row(store, spec, prompt, project_id, handoff_id, None)
    argv = build_bg_argv(replace(spec, prompt=prompt, session_id=None), claude=claude)
    try:
        proc = run(argv, capture_output=True, text=True, cwd=spec.project_path)
    except OSError as exc:
        return _failed(store, launch_id, handoff_id, f"could not run {claude}: {exc}")
    if proc.returncode != 0:
        return _failed(store, launch_id, handoff_id, _spawn_error(claude, proc))

    short_id = parse_bg_handle((proc.stdout or "") + (proc.stderr or ""))
    if short_id is None:
        # It started. Saying otherwise would leave the handoff queued for a
        # session that is running, which is the worse of the two wrong answers.
        return _started(
            store, cfg, launch_id, handoff_id,
            note="started, but no `backgrounded · <short>` handle was printed, "
                 "so this launch has no session id to correlate on",
        )

    session_id = resolve_short_id(short_id, claude, run)
    store.set_launch_session(launch_id, session_id, short_id)
    return _started(
        store, cfg, launch_id, handoff_id,
        session_id=session_id, short_id=short_id,
        note=None if session_id else
        f"short id {short_id} not yet resolvable to a session id; the next index "
        "backfills it from a unique prefix match",
    )


def _started(store, cfg, launch_id, handoff_id, session_id=None, short_id=None,
             note=None) -> LaunchResult:
    store.set_launch_outcome(launch_id, "started")
    if handoff_id:
        # Journal first, then update: the journal is what survives
        # `rm ~/.bridge/bridge.db`, so it must never lag the database it rebuilds.
        try:
            spool.journal_status(handoff_id, "consumed", now_epoch(), cfg.spool_dir)
        except Exception as exc:  # noqa: BLE001 - a running session is not undone
            note = f"{note + '; ' if note else ''}status journal failed: {exc!r}"
        store.set_handoff_status(handoff_id, "consumed")
    return LaunchResult(launch_id, "started", session_id, short_id, None, note)


def _failed(store, launch_id, handoff_id, error) -> LaunchResult:
    """The handoff is left `queued` — deliberately, and that is the whole contract.

    The launcher's promise is only that a failure does not consume; putting the
    prompt on the clipboard belongs to the caller, which is the layer that has a
    clipboard. `launch()` claims the handoff (-> `launching`) before spawning,
    so a failed spawn must explicitly hand it back rather than relying on
    consumption never having happened.
    """
    store.set_launch_outcome(launch_id, "failed")
    if handoff_id:
        store.revert_claimed_handoff(handoff_id)
    return LaunchResult(launch_id, "failed", error=error or "launch failed")


def _is_session_id_collision(proc) -> bool:
    text = strip_ansi((proc.stderr or "") + (proc.stdout or ""))
    return bool(SESSION_ID_IN_USE_RE.search(text))


def _spawn_error(program: str, proc) -> str:
    text = strip_ansi((proc.stderr or "").strip() or (proc.stdout or "").strip())
    detail = f": {text[:500]}" if text else ""
    return f"{program} exited {proc.returncode}{detail}"
