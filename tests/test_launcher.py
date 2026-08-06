"""Command construction, every escaping decision in it, and the spawn.

The central property is an ABSENCE: the prompt is not in the constructed shell
command at all. `/handoff` shipped the presence-only version of this bug in
Phase 2 -- a summary containing `$(...)` was executed -- so the assertions here
are `not in` first and `in` second.

**No test in this module starts a real Claude session or opens a real Terminal
window.** The only executables any of it runs are `/bin/sh`, `/usr/bin/osascript`
(only to round-trip a quoted string, never `do script`), and a fake `claude`
shim written per-test that records its argv and exits. `launch()` takes its
process runner by injection precisely so that stays true: the terminal-mode
tests substitute `run`, so `osascript` is never invoked with a `do script`
payload at all.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from bridge import launcher
from bridge.config import load
from bridge.launcher import (
    MAX_PROMPT_BYTES,
    MAX_SESSION_ID_ATTEMPTS,
    MODES,
    SESSION_ID_RE,
    LaunchError,
    LaunchSpec,
    as_quote,
    build_applescript,
    build_bg_argv,
    build_shell_command,
    default_title,
    gc_prompt_files,
    new_session_id,
    parse_bg_handle,
    resolve_claude,
    sanitize_title,
    sh_quote,
    strip_ansi,
    validate_prompt,
)
from bridge.models import Handoff
from bridge.registry import resolve_project
from bridge.store import Store
from tests.conftest import RealBridgeDirTouched, launch_by_session

# --- Phase 3: the launcher ---------------------------------------------------

# A path with a space and a quote, because the real measurement was taken from a
# project directory named `proj dir's "na\me"`.
CLAUDE = "/opt/fake bin/claude"
PROJECT = "/Users/mitsheth/dev/proj dir's \"na\\me\""
SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PROMPT_FILE = "/Users/mitsheth/.bridge/launches/aaaaaaaa.prompt"

HOSTILE_PROMPT = """Continue the phase.

Costs: $(echo PWNED) and `echo BACKTICK_EXECUTED` and ${HOME} and 'quoted' and
"double quoted" and a trailing backslash \\ must all survive. 🚀 中文 !! ;rm -rf
"""

OSASCRIPT = Path("/usr/bin/osascript")


def spec(**kw) -> LaunchSpec:
    base = dict(
        project_path=PROJECT,
        prompt=HOSTILE_PROMPT,
        session_id=SESSION_ID,
        model="opus",
        effort="high",
        title="Phase 3 launcher",
        mode="terminal",
    )
    base.update(kw)
    return LaunchSpec(**base)


def sh_roundtrip(quoted: str) -> bytes:
    """What `/bin/sh` actually passes to `printf` for `quoted`."""
    proc = subprocess.run(
        ["/bin/sh", "-c", "printf %s " + quoted], capture_output=True
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# --- the prompt never reaches the command line -------------------------------


def test_the_shell_command_does_not_contain_the_prompt():
    """The single most important assertion in the phase.

    A presence-only assertion cannot catch injection, so this is `not in`.
    """
    command = build_shell_command(spec(), PROMPT_FILE, claude=CLAUDE)

    assert HOSTILE_PROMPT not in command
    for fragment in (
        "$(echo PWNED)",
        "echo BACKTICK_EXECUTED",
        "${HOME}",
        "rm -rf",
        "Continue the phase.",
    ):
        assert fragment not in command, f"{fragment!r} leaked into the command"

    # What IS there: a read of the file, in double quotes so the substitution is
    # one argv element rather than however many words the prompt contains.
    assert f"\"$(/bin/cat '{PROMPT_FILE}')\"" in command


def test_the_command_contains_the_readable_file_guard():
    """Without it, `cat` fails, `$?` stays 0, and claude starts with b''."""
    command = build_shell_command(spec(), PROMPT_FILE, claude=CLAUDE)
    assert command.startswith(f"[ -r '{PROMPT_FILE}' ] || ")
    assert "exit 1" in command
    # The guard runs before the cd, or a bad project path masks it.
    assert command.index("[ -r") < command.index("cd ")


def test_the_command_shape_is_the_measured_one():
    command = build_shell_command(spec(), PROMPT_FILE, claude=CLAUDE)
    assert command == (
        f"[ -r '{PROMPT_FILE}' ] || "
        "{ echo 'bridge: prompt file missing' >&2; exit 1; }; "
        "cd '/Users/mitsheth/dev/proj dir'\\''s \"na\\me\"' && "
        "'/opt/fake bin/claude' "
        f"--session-id {SESSION_ID} "
        "--model 'opus' --effort 'high' -n 'Phase 3 launcher' "
        f"\"$(/bin/cat '{PROMPT_FILE}')\""
    )


# --- sh_quote, against a real shell ------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "plain",
        "it's",
        "'",
        "''",
        '"',
        '"double"',
        "`echo BACKTICK`",
        "$(echo SUB)",
        "${HOME}",
        "$HOME",
        "back\\slash",
        "trailing\\",
        "two\nlines",
        "semi;colon && pipe | amp",
        "bang!",
        "bang!!",
        "🚀 emoji",
        "中文",
        "  leading and trailing  ",
        "a'b\"c`d$e\\f;g",
        "proj dir's \"na\\me\"",
    ],
)
def test_sh_quote_round_trips_through_a_real_shell(raw):
    assert sh_roundtrip(sh_quote(raw)) == raw.encode()


def test_the_naive_quoting_would_not_round_trip():
    """Pins WHY sh_quote is written the way it is.

    `s.replace("'", "\\\\'")` is the tempting one-liner. Inside single quotes a
    backslash is literal, so it does not escape anything and the shell sees an
    unterminated quote.
    """
    naive = "'" + "it's".replace("'", "\\'") + "'"
    proc = subprocess.run(
        ["/bin/sh", "-c", "printf %s " + naive], capture_output=True
    )
    assert proc.stdout != b"it's"


# --- as_quote, against the real AppleScript parser ---------------------------


def test_as_quote_escapes_backslash_and_double_quote_without_double_escaping():
    assert as_quote("plain") == '"plain"'
    # Backslash first: one backslash in, two out.
    assert as_quote("a\\b") == '"a\\\\b"'
    # A bare double quote gets exactly one backslash. Escaping `"` before `\`
    # would yield `"\\""` -- a doubled backslash and a literal that ends early.
    assert as_quote('"') == '"\\""'
    assert as_quote('say "hi"') == '"say \\"hi\\""'
    assert as_quote("a\\\"b") == '"a\\\\\\"b"'


def test_as_quote_leaves_shell_metacharacters_alone():
    """`$`, backtick and `!` are not special to AppleScript.

    Touching them here would corrupt a command the shell layer has already made
    safe with single quotes.
    """
    for raw in ("$HOME", "${HOME}", "$(echo x)", "`echo x`", "!!", "bang!"):
        assert as_quote(raw) == f'"{raw}"'


@pytest.mark.parametrize("bad", ["two\nlines", "carriage\rreturn", "/tmp/a\nb/proj"])
def test_as_quote_raises_rather_than_stripping_a_line_break(bad):
    """A newline in a project path is legal in APFS and unrepresentable here."""
    with pytest.raises(LaunchError, match="newline or carriage return"):
        as_quote(bad)


@pytest.mark.skipif(not OSASCRIPT.exists(), reason="osascript not present")
def test_as_quote_round_trips_through_osascript():
    """The shell layer nested in the AppleScript layer, parsed for real."""
    command = build_shell_command(spec(), PROMPT_FILE, claude=CLAUDE)
    proc = subprocess.run(
        [str(OSASCRIPT), "-e", "return " + as_quote(command)], capture_output=True
    )
    assert proc.returncode == 0, proc.stderr
    # osascript appends one newline to the returned string.
    assert proc.stdout.rstrip(b"\n") == command.encode()


def test_build_applescript_runs_the_command_in_a_new_terminal_window():
    script = build_applescript("echo hi")
    assert 'tell application "Terminal"' in script
    assert 'do script "echo hi"' in script
    assert script.rstrip().endswith("end tell")


# --- background argv ---------------------------------------------------------


def test_build_bg_argv_puts_the_prompt_in_exactly_one_element():
    argv = build_bg_argv(spec(mode="background", session_id=None), claude=CLAUDE)
    assert "--bg" in argv
    assert argv[0] == CLAUDE
    assert argv[-1] == HOSTILE_PROMPT
    assert [a for a in argv if a == HOSTILE_PROMPT] == [HOSTILE_PROMPT]
    # No element is a shell string that happens to embed the prompt.
    assert [a for a in argv[:-1] if HOSTILE_PROMPT in a] == []
    assert argv == [
        CLAUDE, "--bg", "--model", "opus", "--effort", "high",
        "-n", "Phase 3 launcher", HOSTILE_PROMPT,
    ]


def test_build_bg_argv_never_passes_session_id():
    """`--bg` mints its own id and warns: "--bg manages the session id".

    Passing it is noise that also implies a correlation this code does not have.
    The spec is factually wrong on this point; see the plan's Self-Review §4.
    """
    argv = build_bg_argv(spec(mode="background"), claude=CLAUDE)
    assert "--session-id" not in argv
    assert SESSION_ID not in argv
    assert not any(SESSION_ID in a for a in argv)


def test_build_bg_argv_keeps_the_name_as_the_human_tie_back():
    argv = build_bg_argv(spec(mode="background", title="Bridge Phase 3"), claude=CLAUDE)
    assert argv[argv.index("-n") + 1] == "Bridge Phase 3"


# --- optional flags are omitted, never emitted empty -------------------------


def test_model_and_effort_are_absent_when_none():
    bare = spec(model=None, effort=None, title=None)

    command = build_shell_command(bare, PROMPT_FILE, claude=CLAUDE)
    for flag in ("--model", "--effort", "-n"):
        assert flag not in command
    assert "''" not in command.replace("'\\''", "")

    argv = build_bg_argv(bare, claude=CLAUDE)
    for flag in ("--model", "--effort", "-n"):
        assert flag not in argv
    assert "" not in argv
    assert argv == [CLAUDE, "--bg", HOSTILE_PROMPT]


def test_a_blank_title_is_omitted_rather_than_emitted_empty():
    """A title that sanitises down to nothing is no title at all."""
    blank = spec(title="\x1b\x07\n")
    assert "-n" not in build_shell_command(blank, PROMPT_FILE, claude=CLAUDE)
    assert "-n" not in build_bg_argv(blank, claude=CLAUDE)


# --- title sanitising -------------------------------------------------------


def test_a_title_with_control_characters_is_sanitised_before_quoting():
    """`-n` has no validation and lands in the terminal-title escape sequence."""
    nasty = "Phase\x1b[2J 3\x07 launcher\nrm -rf /"
    command = build_shell_command(spec(title=nasty), PROMPT_FILE, claude=CLAUDE)

    assert "\x1b" not in command
    assert "\x07" not in command
    assert "\n" not in command
    assert "rm -rf /" not in command  # the second line is not part of the title
    assert "-n 'Phase[2J 3 launcher'" in command

    argv = build_bg_argv(spec(mode="background", title=nasty), claude=CLAUDE)
    assert argv[argv.index("-n") + 1] == "Phase[2J 3 launcher"


def test_sanitize_title_truncates():
    assert sanitize_title("x" * 200) == "x" * 60
    assert sanitize_title("x" * 200, limit=10) == "x" * 10


def test_default_title_prefers_the_summary_first_line_then_the_project_name():
    assert default_title("Built Task 2\nand more", "bridge") == "Built Task 2"
    assert default_title(None, "bridge") == "bridge"
    assert default_title("", "bridge") == "bridge"
    assert default_title("\x1b\x07", "bridge") == "bridge"


# --- session ids ------------------------------------------------------------


def test_generated_session_ids_are_lowercase_and_pass_claudes_validator():
    """Uppercase passes claude's case-insensitive gate, then collides on APFS.

    The id we record and the transcript filename would disagree, which reads as
    a session that never started.
    """
    for _ in range(50):
        sid = new_session_id()
        assert sid == sid.lower()
        assert SESSION_ID_RE.match(sid), sid
    assert len(set(new_session_id() for _ in range(50))) == 50


def test_an_uppercase_session_id_is_refused_by_the_shell_builder():
    """It goes onto the command line unquoted, so it is validated, not trusted."""
    with pytest.raises(LaunchError, match="8-4-4-4-12"):
        build_shell_command(spec(session_id=SESSION_ID.upper()), PROMPT_FILE,
                            claude=CLAUDE)
    with pytest.raises(LaunchError, match="8-4-4-4-12"):
        build_shell_command(spec(session_id="; rm -rf /"), PROMPT_FILE, claude=CLAUDE)


# --- resolving claude -------------------------------------------------------


def test_resolve_claude_raises_when_it_is_not_on_path():
    """Hardcoding the path is what would make every fake-on-PATH test vacuous."""
    with pytest.raises(LaunchError, match="not on PATH"):
        resolve_claude(which=lambda _name: None)


def test_resolve_claude_returns_what_which_found():
    seen = []

    def which(name):
        seen.append(name)
        return "/somewhere/else/claude"

    assert resolve_claude(which=which) == "/somewhere/else/claude"
    assert seen == ["claude"]


# --- prompts that cannot survive argv ---------------------------------------


def test_a_prompt_containing_nul_is_refused():
    """`before\\x00after` arrives as b'before'. Unavoidable, so reject it."""
    with pytest.raises(LaunchError, match="NUL"):
        validate_prompt("before\x00after")


def test_a_prompt_over_the_cap_is_refused_and_the_error_names_the_numbers():
    over = "x" * (MAX_PROMPT_BYTES + 1)
    with pytest.raises(LaunchError) as excinfo:
        validate_prompt(over)
    message = str(excinfo.value)
    assert str(MAX_PROMPT_BYTES) in message
    assert str(len(over)) in message
    validate_prompt("x" * MAX_PROMPT_BYTES)  # the cap itself is allowed


def test_the_cap_counts_bytes_not_characters():
    """A 3-byte character must count as 3. ARG_MAX is measured in bytes."""
    with pytest.raises(LaunchError, match="over the"):
        validate_prompt("中" * (MAX_PROMPT_BYTES // 3 + 1))


@pytest.mark.parametrize("bad", ["with a \x00 nul", "x" * (MAX_PROMPT_BYTES + 1)])
def test_a_bad_prompt_is_refused_before_anything_is_constructed(bad, monkeypatch):
    """Nothing is resolved, quoted or built for a prompt that cannot launch.

    `resolve_claude` is booby-trapped, so a builder that validates late fails
    here with the wrong exception type.
    """
    def trap(*a, **k):
        raise AssertionError("resolved claude before validating the prompt")

    monkeypatch.setattr("bridge.launcher.resolve_claude", trap)

    with pytest.raises(LaunchError):
        build_shell_command(spec(prompt=bad), PROMPT_FILE)
    with pytest.raises(LaunchError):
        build_bg_argv(spec(prompt=bad, mode="background"))


# --- the value type ---------------------------------------------------------


def test_launch_spec_carries_the_two_modes_and_nothing_else_is_expected():
    assert MODES == ("terminal", "background")
    assert LaunchSpec(project_path="/p", prompt="hi").mode == "terminal"
    assert LaunchSpec(project_path="/p", prompt="hi").session_id is None


# --- Task 3: the spawn, the prompt file, and the outcome ---------------------
#
# The suite had no fake-executable precedent before this, so the shim below is
# it. Two properties make it meaningful rather than decorative:
#   * `resolve_claude` consults PATH through an injected `which`, so putting the
#     shim on PATH genuinely redirects the launch. `gitprobe`'s hardcoded
#     `/usr/bin/git` is the trap this avoids -- hardcode `claude` the same way
#     and every test here passes while testing nothing.
#   * it records argv through `json.dumps(sys.argv[1:])`, which preserves the
#     bytes exactly: newlines, trailing whitespace, emoji and CJK all survive
#     the round trip back into the test.
# PATH is set explicitly in the fixture, never inherited, because `tools/falsify.py`
# runs pytest under `PATH=/usr/bin:/bin`.

FAKE_CLAUDE_SOURCE = '''"""A `claude` that records its argv, then does what the env says.

Never a real session: this shim is the entire verification surface for the
launcher's spawn path.
"""
import json
import os
import sys

with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")

if sys.argv[1:2] == ["agents"]:
    agents = os.environ.get("FAKE_CLAUDE_AGENTS", "")
    sys.stdout.write(agents)
    sys.exit(0 if agents else 1)

sys.stdout.write(os.environ.get("FAKE_CLAUDE_STDOUT", ""))
sys.stderr.write(os.environ.get("FAKE_CLAUDE_STDERR", ""))
sys.exit(int(os.environ.get("FAKE_CLAUDE_RC", "0")))
'''

NORMALISED = HOSTILE_PROMPT.rstrip("\n")
BG_SESSION_ID = "deadbeef-a2d0-4d3b-878b-e37f81284385"


class FakeClaude:
    """The shim, plus byte-exact access to every argv it saw."""

    def __init__(self, path: Path, log: Path):
        self.path = path
        self.log = log

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def call_with(self, flag: str) -> list[str]:
        matching = [c for c in self.calls() if flag in c]
        assert matching, f"no fake-claude invocation carried {flag!r}: {self.calls()}"
        return matching[-1]

    def env(self) -> dict[str, str]:
        """The environment a `/bin/sh` child needs to reach the shim's log."""
        return dict(os.environ)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch) -> FakeClaude:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    exe = bindir / "claude"
    # The interpreter is absolute in the shebang, so the shim runs under the
    # falsification harness's PATH=/usr/bin:/bin as well as under a normal one.
    exe.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE_SOURCE}")
    exe.chmod(0o755)

    log = tmp_path / "fake-claude-argv.jsonl"
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    for name in ("FAKE_CLAUDE_STDOUT", "FAKE_CLAUDE_STDERR", "FAKE_CLAUDE_RC",
                 "FAKE_CLAUDE_AGENTS"):
        monkeypatch.delenv(name, raising=False)
    return FakeClaude(exe, log)


@pytest.fixture
def cfg(tmp_path):
    return load({
        "db_path": tmp_path / "b.db",
        "spool_dir": tmp_path / "spool",
        "launches_dir": tmp_path / "launches",
    })


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


@pytest.fixture
def project(tmp_path) -> Path:
    """A real directory named the way the hostile measurement's was."""
    p = tmp_path / 'proj dir\'s "na\\me"'
    p.mkdir()
    return p


def proc(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout,
                                       stderr=stderr)


def recorder(*results):
    """A `run` double. Records argv; the last result repeats indefinitely.

    Every terminal-mode test goes through this instead of `subprocess.run`, which
    is what keeps `osascript`'s `do script` -- and therefore a real Terminal
    window and a real Claude session -- out of the suite entirely.
    """
    results = results or (proc(),)
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return results[min(len(calls) - 1, len(results) - 1)]

    run.calls = calls
    return run


def queue_handoff(store, project_path: str, hid="h-launch") -> str:
    store.create_handoff(
        Handoff(id=hid, project_path=project_path, next_prompt=HOSTILE_PROMPT,
                summary="Built Task 2", created_at=1000),
        resolve_project(store, project_path),
    )
    return hid


def status_records(cfg) -> list[dict]:
    drained = Path(cfg.spool_dir) / "drained"
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(drained.glob("*.status.json"))
    ] if drained.is_dir() else []


def only_launch(store, project_path):
    rows = store.launches(resolve_project(store, project_path))
    assert len(rows) == 1, f"expected exactly one launch row, got {len(rows)}"
    return rows[0]


# --- the prompt arrives byte for byte, in both modes -------------------------


def test_background_mode_delivers_the_prompt_byte_for_byte(
    store, cfg, project, fake_claude, monkeypatch
):
    """One argv element, no shell, no quoting layer to survive."""
    monkeypatch.setenv("FAKE_CLAUDE_STDOUT", "backgrounded · deadbeef\n")

    result = launcher.launch(
        store, cfg,
        spec(project_path=str(project), mode="background", session_id=None),
    )

    assert result.outcome == "started", result.error
    argv = fake_claude.call_with("--bg")
    assert argv[-1] == NORMALISED
    assert argv[-1].encode("utf-8") == NORMALISED.encode("utf-8")
    # Absence, not just presence: nothing quoted it, nothing split it.
    assert [a for a in argv[:-1] if NORMALISED in a] == []
    assert "--session-id" not in argv
    for fragment in ("$(echo PWNED)", "`echo BACKTICK_EXECUTED`", "${HOME}",
                     "'quoted'", '"double quoted"', "🚀 中文", "\n"):
        assert fragment in argv[-1], f"{fragment!r} did not survive argv"
    assert "PWNED\n" not in argv[-1] and argv[-1].count("PWNED") == 1


def test_the_terminal_shell_layer_delivers_the_prompt_byte_for_byte(
    store, cfg, project, fake_claude
):
    """The shell half of terminal mode, run for real through `/bin/sh -c`.

    No AppleScript and no Terminal window: `launch` writes the prompt file with
    an injected `run`, and the command that file feeds is then executed directly.
    """
    run = recorder(proc(0))
    result = launcher.launch(
        store, cfg, spec(project_path=str(project), session_id=None), run=run
    )
    assert result.outcome == "started", result.error

    # The script is ONE argv element, never a shell string.
    assert len(run.calls) == 1
    assert run.calls[0][:2] == ["/usr/bin/osascript", "-e"]
    assert len(run.calls[0]) == 3
    assert NORMALISED not in run.calls[0][2]

    prompt_path = Path(cfg.launches_dir) / f"{result.session_id}.prompt"
    assert prompt_path.read_text(encoding="utf-8") == NORMALISED

    command = build_shell_command(
        spec(project_path=str(project), session_id=result.session_id),
        prompt_path, claude=str(fake_claude.path),
    )
    assert NORMALISED not in command  # the central absence, again
    sh = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True,
                        env=fake_claude.env())
    assert sh.returncode == 0, sh.stderr

    argv = fake_claude.calls()[-1]
    assert argv[-1] == NORMALISED
    assert argv[-1].encode("utf-8") == NORMALISED.encode("utf-8")
    # Nothing was executed, and nothing was word-split.
    assert argv.count(NORMALISED) == 1
    assert "$(echo PWNED)" in argv[-1]
    assert argv[-1].count("PWNED") == 1
    assert "`echo BACKTICK_EXECUTED`" in argv[-1]
    assert "BACKTICK_EXECUTED" in argv[-1] and argv[-1].count("BACKTICK_EXECUTED") == 1
    assert "${HOME}" in argv[-1] and str(Path.home()) not in argv[-1]


def test_a_literal_command_substitution_arrives_literal(store, cfg, project,
                                                        fake_claude):
    """`$(echo nope)` must reach claude as nine characters, not as `nope`."""
    prompt = "before $(echo nope) after"
    run = recorder(proc(0))
    result = launcher.launch(
        store, cfg,
        spec(project_path=str(project), prompt=prompt, session_id=None),
        run=run,
    )
    prompt_path = Path(cfg.launches_dir) / f"{result.session_id}.prompt"
    command = build_shell_command(
        spec(project_path=str(project), prompt=prompt,
             session_id=result.session_id),
        prompt_path, claude=str(fake_claude.path),
    )
    sh = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True,
                        env=fake_claude.env())
    assert sh.returncode == 0, sh.stderr
    assert fake_claude.calls()[-1][-1] == "before $(echo nope) after"


def test_a_missing_prompt_file_launches_nothing_at_all(project, fake_claude,
                                                       tmp_path):
    """The `[ -r ]` guard, checked by its consequence.

    Without it `cat` fails, the shell's exit status stays 0, and claude starts
    with an EMPTY prompt -- observed as `argc=4` with a final `b''`. A silent
    empty session is the worst failure available here because it looks like it
    worked, so the assertion is that the fake recorded NOTHING.
    """
    missing = tmp_path / "launches" / "never-written.prompt"
    command = build_shell_command(
        spec(project_path=str(project)), missing, claude=str(fake_claude.path)
    )
    sh = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True,
                        env=fake_claude.env())

    assert sh.returncode != 0
    assert "prompt file missing" in sh.stderr
    assert fake_claude.calls() == [], "claude ran despite a missing prompt file"


# --- outcomes, rows, and the handoff ----------------------------------------


def test_a_failed_spawn_records_failed_and_leaves_the_handoff_queued(
    store, cfg, project, fake_claude):
    """Losing the prompt is the worst outcome in this phase, so it is pinned."""
    hid = queue_handoff(store, str(project))
    run = recorder(proc(1, stderr="osascript: execution error"))

    result = launcher.launch(
        store, cfg, spec(project_path=str(project), session_id=None), hid, run=run
    )

    assert result.outcome == "failed"
    assert result.error and "execution error" in result.error
    row = store.get_handoff(hid)
    assert row["status"] == "queued"
    assert row["consumed_at"] is None
    assert status_records(cfg) == []
    assert store.queued_handoff(resolve_project(store, str(project))) is not None


def test_a_launches_row_exists_even_when_the_spawn_fails(store, cfg, project,
                                                        fake_claude):
    """The row is written BEFORE the spawn, so a failure is still correlatable."""
    result = launcher.launch(
        store, cfg, spec(project_path=str(project), session_id=None),
        run=recorder(proc(1, stderr="boom")),
    )
    row = only_launch(store, str(project))
    assert row["id"] == result.launch_id
    assert row["outcome"] == "failed"
    assert row["mode"] == "terminal"
    # The pre-assigned id is on the row even though the result reports no session:
    # that is what makes a failed launch correlatable at all.
    assert result.session_id is None
    assert SESSION_ID_RE.match(row["session_id"])
    assert row["prompt"] == NORMALISED  # provenance: exactly what would have run


def test_the_prompt_file_survives_a_successful_launch(store, cfg, project, fake_claude):
    """`do script` returns immediately; `cat` runs later, in the new shell.

    Deleting the file when `osascript` returns is a live race whose prize is the
    empty session above. It is also provenance, so it is kept and GC'd by age.
    """
    result = launcher.launch(
        store, cfg, spec(project_path=str(project), session_id=None),
        run=recorder(proc(0)),
    )
    prompt_path = Path(cfg.launches_dir) / f"{result.session_id}.prompt"
    assert prompt_path.is_file()
    assert prompt_path.read_text(encoding="utf-8") == NORMALISED


def test_a_successful_launch_consumes_and_journals_the_handoff(store, cfg, project,
                                                              fake_claude):
    hid = queue_handoff(store, str(project))

    result = launcher.launch(
        store, cfg, spec(project_path=str(project), session_id=None), hid,
        run=recorder(proc(0)),
    )

    assert result.outcome == "started"
    row = store.get_handoff(hid)
    assert row["status"] == "consumed"
    assert row["consumed_at"] is not None
    assert store.queued_handoff(resolve_project(store, str(project))) is None
    # And the journal, so `rm ~/.bridge/bridge.db && bridge index` does not put a
    # prompt you already ran back at the top of the dashboard.
    assert status_records(cfg) == [
        {"handoff_id": hid, "status": "consumed",
         "at": status_records(cfg)[0]["at"]}
    ]
    assert only_launch(store, str(project))["handoff_id"] == hid


def test_the_launch_row_carries_mode_model_effort_and_outcome(store, cfg, project,
                                                             fake_claude):
    launcher.launch(
        store, cfg,
        spec(project_path=str(project), mode="terminal", session_id=None,
             model="opus", effort="high"),
        run=recorder(proc(0)),
    )
    row = only_launch(store, str(project))
    assert (row["mode"], row["model"], row["effort"], row["outcome"]) == (
        "terminal", "opus", "high", "started",
    )
    assert row["launched_at"] > 0


# --- background correlation -------------------------------------------------


def test_a_colour_wrapped_handle_is_stripped_to_eight_hex(store, cfg, project,
                                                          fake_claude, monkeypatch):
    """Unstripped, `\\x1b[36mdeadbeef\\x1b[0m` matches no glob and no lookup."""
    monkeypatch.setenv("FAKE_CLAUDE_STDOUT", "backgrounded · \x1b[36mdeadbeef\x1b[0m\n")

    result = launcher.launch(
        store, cfg,
        spec(project_path=str(project), mode="background", session_id=None),
    )

    assert result.outcome == "started", result.error
    assert result.short_id == "deadbeef"
    row = only_launch(store, str(project))
    assert row["short_id"] == "deadbeef"
    # The full id is best effort, and this fake `agents` refuses, so it stays null
    # for Task 7's prefix backfill to fill in.
    assert row["session_id"] is None
    assert result.session_id is None
    assert result.note and "backfill" in result.note


def test_a_resolvable_handle_becomes_a_full_session_id(store, cfg, project,
                                                       fake_claude, monkeypatch):
    """`agents --json --all` pairs `"id": "deadbeef"` with its `sessionId`."""
    monkeypatch.setenv("FAKE_CLAUDE_STDOUT", "backgrounded · deadbeef · phase-3\n")
    monkeypatch.setenv(
        "FAKE_CLAUDE_AGENTS",
        json.dumps([{"id": "cafe0000", "sessionId": "cafe0000-" + BG_SESSION_ID[9:]},
                    {"id": "deadbeef", "sessionId": BG_SESSION_ID}]),
    )

    result = launcher.launch(
        store, cfg,
        spec(project_path=str(project), mode="background", session_id=None),
    )

    assert (result.short_id, result.session_id) == ("deadbeef", BG_SESSION_ID)
    row = only_launch(store, str(project))
    assert row["session_id"] == BG_SESSION_ID
    assert launch_by_session(store, BG_SESSION_ID)["id"] == result.launch_id
    assert result.note is None


def test_an_unparseable_background_handle_is_still_started(store, cfg, project,
                                                           fake_claude, monkeypatch):
    """It DID start. Marking it failed would requeue a handoff for a live session."""
    hid = queue_handoff(store, str(project))
    monkeypatch.setenv("FAKE_CLAUDE_STDOUT", "some unexpected output\n")

    result = launcher.launch(
        store, cfg,
        spec(project_path=str(project), mode="background", session_id=None),
        hid,
    )

    assert result.outcome == "started"
    assert result.session_id is None and result.short_id is None
    assert result.note and "no `backgrounded" in result.note
    row = only_launch(store, str(project))
    assert row["session_id"] is None and row["short_id"] is None
    assert row["outcome"] == "started"
    assert store.get_handoff(hid)["status"] == "consumed"


def test_a_background_spawn_that_exits_non_zero_fails(store, cfg, project,
                                                       fake_claude, monkeypatch):
    hid = queue_handoff(store, str(project))
    monkeypatch.setenv("FAKE_CLAUDE_RC", "2")
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "claude: no such model\n")

    result = launcher.launch(
        store, cfg,
        spec(project_path=str(project), mode="background", session_id=None),
        hid,
    )

    assert result.outcome == "failed"
    assert "no such model" in result.error
    assert store.get_handoff(hid)["status"] == "queued"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("backgrounded · deadbeef", "deadbeef"),
        ("backgrounded · deadbeef · a name", "deadbeef"),
        ("\x1b[2mbackgrounded\x1b[0m · \x1b[36m00b31445\x1b[0m", "00b31445"),
        ("noise\nbackgrounded · 0123abcd\nmore", "0123abcd"),
        ("backgrounded · DEADBEEF", None),      # claude emits lowercase
        ("backgrounded · deadbeef1", None),     # not eight
        ("nothing to see here", None),
    ],
)
def test_parse_bg_handle_matches_only_eight_lowercase_hex(line, expected):
    assert parse_bg_handle(line) == expected


def test_strip_ansi_removes_colour_without_touching_the_text():
    assert strip_ansi("\x1b[36mdeadbeef\x1b[0m") == "deadbeef"
    assert strip_ansi("plain $(echo x) `y` 中文") == "plain $(echo x) `y` 中文"


# --- a session id collision is recoverable ----------------------------------


COLLISION = (
    "Error: Session ID aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee is already in use."
)


def test_a_session_id_collision_retries_with_a_fresh_uuid(store, cfg, project,
                                                          fake_claude):
    """`claude` `statSync`s `<project-dir>/<uuid>.jsonl`; a hit exits 1.

    That is recoverable by minting another id, so treating it as a launch failure
    would fail for a reason the launcher can fix by itself.
    """
    run = recorder(proc(1, stderr=COLLISION), proc(0))

    result = launcher.launch(
        store, cfg, spec(project_path=str(project), session_id=None), run=run
    )

    assert result.outcome == "started", result.error
    assert len(run.calls) == 2
    row = only_launch(store, str(project))
    assert row["outcome"] == "started"
    assert row["session_id"] == result.session_id
    assert SESSION_ID_RE.match(row["session_id"])
    # A fresh id, and its prompt file was written before the retry spawned.
    first, second = (c[2] for c in run.calls)
    assert first != second
    assert (Path(cfg.launches_dir) / f"{result.session_id}.prompt").is_file()
    assert len(list(Path(cfg.launches_dir).glob("*.prompt"))) == 2


def test_the_collision_retry_is_bounded(store, cfg, project, fake_claude):
    """A systematic cause must not become an unbounded spawn loop."""
    run = recorder(proc(1, stderr=COLLISION))

    result = launcher.launch(
        store, cfg, spec(project_path=str(project), session_id=None), run=run
    )

    assert result.outcome == "failed"
    assert "already in use" in result.error
    assert len(run.calls) == MAX_SESSION_ID_ATTEMPTS
    assert MAX_SESSION_ID_ATTEMPTS <= 5


def test_a_non_collision_failure_is_not_retried(store, cfg, project, fake_claude):
    run = recorder(proc(1, stderr="osascript: -1743 not authorised"))
    result = launcher.launch(
        store, cfg, spec(project_path=str(project), session_id=None), run=run
    )
    assert result.outcome == "failed"
    assert len(run.calls) == 1


# --- refusals happen before any side effect ---------------------------------


def test_launch_refuses_when_claude_is_not_on_path(store, cfg, project, tmp_path):
    """The injected `which` genuinely searches a directory, and finds nothing.

    Hardcoding the path is what would make every fake-on-PATH test above vacuous.
    """
    empty = tmp_path / "empty bin"
    empty.mkdir()
    ran = recorder(proc(0))

    with pytest.raises(LaunchError, match="not on PATH"):
        launcher.launch(
            store, cfg, spec(project_path=str(project), session_id=None),
            run=ran, which=lambda name: shutil.which(name, path=str(empty)),
        )

    assert ran.calls == []
    assert store.launches(resolve_project(store, str(project))) == []
    assert not Path(cfg.launches_dir).exists()


def test_launch_finds_the_fake_claude_through_the_injected_which(
    store, cfg, project, fake_claude, tmp_path
):
    """The positive half: the same `which` on a directory that DOES hold it."""
    resolved = shutil.which("claude", path=str(fake_claude.path.parent))
    assert resolved == str(fake_claude.path)
    result = launcher.launch(
        store, cfg, spec(project_path=str(project), session_id=None),
        run=recorder(proc(0)),
        which=lambda name: shutil.which(name, path=str(fake_claude.path.parent)),
    )
    assert result.outcome == "started"


@pytest.mark.parametrize("bad", ["with a \x00 nul", "x" * (MAX_PROMPT_BYTES + 1)])
def test_a_prompt_that_cannot_launch_leaves_no_trace(store, cfg, project, bad):
    ran = recorder(proc(0))
    with pytest.raises(LaunchError):
        launcher.launch(
            store, cfg,
            spec(project_path=str(project), prompt=bad, session_id=None),
            run=ran, which=lambda _n: "/opt/fake bin/claude",
        )
    assert ran.calls == []
    assert store.launches(resolve_project(store, str(project))) == []
    assert not Path(cfg.launches_dir).exists()


def test_an_unknown_mode_is_refused(store, cfg, project):
    with pytest.raises(LaunchError, match="mode"):
        launcher.launch(
            store, cfg, spec(project_path=str(project), mode="tmux"),
            run=recorder(proc(0)), which=lambda _n: "/opt/fake bin/claude",
        )


# --- the prompt file on disk ------------------------------------------------


def test_the_prompt_file_is_0600_inside_a_0700_directory(store, cfg, project,
                                                        fake_claude):
    """The prompt is now at rest on disk. That is the accepted cost of the RCE fix."""
    result = launcher.launch(
        store, cfg, spec(project_path=str(project), session_id=None),
        run=recorder(proc(0)),
    )
    directory = Path(cfg.launches_dir)
    prompt_path = directory / f"{result.session_id}.prompt"

    assert stat.S_IMODE(prompt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    # No temp file left behind, and nothing else in there.
    assert sorted(p.name for p in directory.iterdir()) == [prompt_path.name]


def test_the_prompt_file_is_normalised_so_both_modes_agree(store, cfg, project,
                                                           fake_claude, monkeypatch):
    """`$(cat)` strips trailing newlines and argv does not. One standard, not two."""
    prompt = "trailing spaces here   \nand a body\n\n\n"
    monkeypatch.setenv("FAKE_CLAUDE_STDOUT", "backgrounded · deadbeef\n")

    terminal = launcher.launch(
        store, cfg, spec(project_path=str(project), prompt=prompt, session_id=None),
        run=recorder(proc(0)),
    )
    on_disk = (Path(cfg.launches_dir) / f"{terminal.session_id}.prompt").read_text(
        encoding="utf-8"
    )
    launcher.launch(
        store, cfg,
        spec(project_path=str(project), prompt=prompt, mode="background",
             session_id=None),
    )

    assert on_disk == prompt.rstrip("\n")
    assert fake_claude.call_with("--bg")[-1] == on_disk
    assert on_disk.endswith("and a body")
    assert "trailing spaces here   \n" in on_disk  # interior whitespace is kept


def test_gc_removes_stale_prompt_files_and_keeps_fresh_ones(cfg):
    directory = Path(cfg.launches_dir)
    directory.mkdir(parents=True)
    stale, fresh = directory / "old.prompt", directory / "new.prompt"
    for p in (stale, fresh):
        p.write_text("x", encoding="utf-8")
    os.utime(stale, (0, 0))

    assert gc_prompt_files(directory, max_age_days=7) == 1
    assert not stale.exists()
    assert fresh.exists()
    assert gc_prompt_files(directory / "not there") == 0


# --- the conftest guard -----------------------------------------------------


def test_the_conftest_guard_fires_when_launches_dir_is_not_overridden(store, tmp_path):
    """A launch WRITES, so a fixture that forgets the override must fail loudly.

    `RealBridgeDirTouched` derives from `BaseException` on purpose, so no
    well-behaved catch-all can swallow it -- including the ones inside `launch`.
    """
    unguarded = load({"db_path": tmp_path / "b.db", "spool_dir": tmp_path / "spool"})
    assert unguarded.launches_dir == Path.home() / ".bridge" / "launches"
    ran = recorder(proc(0))

    with pytest.raises(RealBridgeDirTouched, match="launches_dir"):
        launcher.launch(
            store, unguarded, spec(project_path=str(tmp_path), session_id=None),
            run=ran, which=lambda _n: "/opt/fake bin/claude",
        )

    assert ran.calls == [], "it spawned before the guard could fire"


def test_the_default_launches_dir_lives_under_the_bridge_dir():
    assert load().launches_dir == Path.home() / ".bridge" / "launches"
    assert load({"launches_dir": Path("/tmp/x")}).launches_dir == Path("/tmp/x")


# --- Phase 4 Task 2: permission modes ----------------------------------------
#
# Widened from the plan's `bypass_permissions` boolean to the CLI's real enum.
# Measured against 2.1.220: `--permission-mode` rejects anything outside
# {acceptEdits, auto, bypassPermissions, manual, dontAsk, plan}. There is NO
# `default` -- that value belongs to settings.json's `permissions.defaultMode`,
# which is a different and smaller set.


def test_terminal_mode_omits_the_permission_flag_by_default():
    """The default must be no flag at all, not a flag with a benign value.

    This is the highest-value assertion in the task: a default that emits
    anything makes every launch carry a permission decision nobody took.
    """
    command = build_shell_command(spec(), PROMPT_FILE, claude=CLAUDE)
    assert "--permission-mode" not in command
    assert "dangerously" not in command


def test_background_mode_omits_the_permission_flag_by_default():
    argv = build_bg_argv(spec(mode="background"), claude=CLAUDE)
    assert not any("permission" in a for a in argv)
    assert not any("dangerously" in a for a in argv)


def test_an_empty_permission_mode_is_the_same_as_none():
    """The select's default option has value="" and posts as an empty string."""
    command = build_shell_command(
        spec(permission_mode=""), PROMPT_FILE, claude=CLAUDE
    )
    assert "--permission-mode" not in command


@pytest.mark.parametrize(
    "mode", ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"]
)
def test_every_mode_the_cli_accepts_is_emitted_verbatim(mode):
    command = build_shell_command(
        spec(permission_mode=mode), PROMPT_FILE, claude=CLAUDE
    )
    assert f"--permission-mode {mode}" in command
    assert command.count("--permission-mode") == 1


@pytest.mark.parametrize(
    "mode", ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"]
)
def test_background_mode_passes_the_mode_as_its_own_argv_element(mode):
    argv = build_bg_argv(spec(mode="background", permission_mode=mode), claude=CLAUDE)
    assert argv.count("--permission-mode") == 1
    assert argv[argv.index("--permission-mode") + 1] == mode
    assert argv[-1] == HOSTILE_PROMPT  # still exactly one element, still last


def test_an_unrecognised_permission_mode_is_refused_not_forwarded():
    """Deliberately unlike `model` and `effort`, which pass through unvalidated.

    Those are capability knobs where the CLI is the authority. This is a SAFETY
    control: a value that is not one of the known modes must not become a flag,
    because the failure mode is a session running with permissions nobody chose.
    Fail closed and construct nothing.
    """
    with pytest.raises(LaunchError):
        build_shell_command(
            spec(permission_mode="bypassPermission"), PROMPT_FILE, claude=CLAUDE
        )
    with pytest.raises(LaunchError):
        build_bg_argv(
            spec(mode="background", permission_mode="yolo"), claude=CLAUDE
        )


def test_the_bypass_mode_is_never_the_allow_variant():
    """`--allow-dangerously-skip-permissions` only makes the mode AVAILABLE.

    Substituting it would produce a launch that looks bypassed and is not.
    """
    argv = build_bg_argv(
        spec(mode="background", permission_mode="bypassPermissions"), claude=CLAUDE
    )
    assert "--allow-dangerously-skip-permissions" not in argv
    command = build_shell_command(
        spec(permission_mode="bypassPermissions"), PROMPT_FILE, claude=CLAUDE
    )
    assert "--allow-dangerously-skip-permissions" not in command


def test_the_emitted_mode_is_a_member_of_a_closed_set_not_caller_text():
    """Validated against a frozen set, so the emitted token can never be
    assembled from or influenced by user input -- the constraint the plan wrote
    for the boolean, carried over to the enum."""
    from bridge.launcher import PERMISSION_MODES

    assert PERMISSION_MODES == frozenset(
        {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
    )
    for hostile in ("plan; rm -rf /", "plan --foo", "plan\nplan", " plan"):
        with pytest.raises(LaunchError):
            build_shell_command(
                spec(permission_mode=hostile), PROMPT_FILE, claude=CLAUDE
            )


# --- there has to be somewhere to run ----------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_a_project_path_that_is_not_a_directory_is_refused(
    store, cfg, tmp_path, fake_claude, mode
):
    """`cd` into a non-directory fails, so a terminal launch opens a real
    window only to die on its first line and a background launch dies inside
    `subprocess.run`'s own `cwd` handling. Both happen AFTER `resolve_project`
    has created a registry row for a project that does not exist, so the panel
    grows a phantom entry out of a typo."""
    missing = tmp_path / "not-here"

    with pytest.raises(LaunchError) as raised:
        launcher.launch(store, cfg, spec(project_path=str(missing), mode=mode))

    assert "is not a directory" in str(raised.value)


def test_the_refusal_writes_no_row_no_prompt_file_and_spawns_nothing(
    store, cfg, tmp_path, fake_claude
):
    """The refusal belongs in the block that runs before any side effect. A
    guard placed one line later would still refuse -- and still leave the
    registry row, which is the actual damage."""
    missing = tmp_path / "gone"
    calls = []

    with pytest.raises(LaunchError):
        launcher.launch(store, cfg, spec(project_path=str(missing)),
                        run=lambda *a, **k: calls.append(a) or proc(0))

    assert calls == []
    assert store.projects() == []
    assert not (Path(cfg.launches_dir).exists()
                and list(Path(cfg.launches_dir).glob("*.prompt")))


def test_a_file_is_not_a_project_directory(store, cfg, tmp_path, fake_claude):
    """`exists()` would not have caught this one, which is why the guard tests
    `is_dir()`."""
    a_file = tmp_path / "README.md"
    a_file.write_text("not a project")

    with pytest.raises(LaunchError):
        launcher.launch(store, cfg, spec(project_path=str(a_file)))
