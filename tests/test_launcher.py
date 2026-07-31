"""Command construction, and every escaping decision in it.

The central property is an ABSENCE: the prompt is not in the constructed shell
command at all. `/handoff` shipped the presence-only version of this bug in
Phase 2 -- a summary containing `$(...)` was executed -- so the assertions here
are `not in` first and `in` second.

No test in this module spawns `claude`. The only subprocesses are `/bin/sh` and
`osascript`, both used to round-trip a quoted string back to Python so the
escaping is checked against the real parsers rather than against a regex.
"""

import subprocess
from pathlib import Path

import pytest

from bridge.launcher import (
    MAX_PROMPT_BYTES,
    MODES,
    SESSION_ID_RE,
    LaunchError,
    LaunchSpec,
    as_quote,
    build_applescript,
    build_bg_argv,
    build_shell_command,
    default_title,
    new_session_id,
    resolve_claude,
    sanitize_title,
    sh_quote,
    validate_prompt,
)

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
