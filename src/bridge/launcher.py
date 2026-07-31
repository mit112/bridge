"""Launch command construction. Pure functions from arguments to strings.

Nothing here writes a file, spawns a process, or opens a database. That split is
the design: every escaping decision lives in a function whose whole contract is
`arguments in, string out`, which is what makes the hostile cases exhaustively
testable.

Measured against the real environment before any of this was written:
  * A terminal launch nests a shell command inside an AppleScript literal inside
    `osascript -e`, and `do script` runs an INTERACTIVE zsh, so history
    expansion is live and a `zsh -c` test would not catch it. The naive inline
    form is a full RCE:
      sent      claude … "INLINE $(echo PWNED) `echo BACKTICK_EXECUTED` ${HOME}"
      received  b'INLINE PWNED BACKTICK_EXECUTED /Users/mitsheth'
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

import re
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CAT = "/bin/cat"

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
    title = sanitize_title(spec.title or "")
    if title:
        argv += ["-n", title]
    argv.append(spec.prompt)  # exactly one element, verbatim
    return argv
