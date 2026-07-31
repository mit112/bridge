"""Round-trip test for the `/handoff` slash command.

The command is a markdown file telling Claude which argv to run, so the argv is
the thing under test. This extracts the bash block straight out of
`commands/handoff.md` and executes it against a real uvicorn server on a real
port. A hand-copied duplicate of the command would keep passing after the
documented one broke.
"""

import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bridge.api import create_app
from bridge.config import load
from bridge.store import Store

REPO = Path(__file__).resolve().parent.parent
SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

REALISTIC_PROMPT = """Continue Bridge Phase 2 in ~/dev/bridge on branch phase2-handoff-loop.

Tasks 1-4 are committed and green. What is left is Task 5, the card UI with
copy-to-clipboard, and Task 6, the droppable backfill.

Do not relitigate these: queue semantics are supersede, the server runs manually
via `bridge serve`, and inline editing is deferred to Phase 3.

Traps worth knowing: `git checkout --` restores to HEAD, so commit before you
falsify; and a mutation that only moves code is byte-size identical, so clear
__pycache__ or stale bytecode keeps running. Costs: $(echo "not expanded") and
`backticks` and ${BRACES} must survive verbatim."""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def command_template() -> str:
    """The first bash block in the command file is the handoff invocation."""
    md = (REPO / "commands" / "handoff.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)```", md, re.S)
    assert blocks, "commands/handoff.md has no bash block"
    assert "bridge handoff" in blocks[0], (
        f"the first bash block is no longer the handoff invocation: {blocks[0][:80]}"
    )
    return blocks[0]


@pytest.fixture
def live_server(tmp_path):
    """A real server on a real port, not a TestClient transport."""
    import uvicorn

    cfg = load({
        "db_path": tmp_path / "live.db",
        "spool_dir": tmp_path / "spool",
        "port": free_port(),
    })
    store = Store(cfg.db_path)
    server = uvicorn.Server(
        uvicorn.Config(create_app(store, cfg), host="127.0.0.1",
                       port=cfg.port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while time.time() < deadline:
        probe = socket.socket()
        probe.settimeout(0.2)
        ok = probe.connect_ex(("127.0.0.1", cfg.port)) == 0
        probe.close()
        if ok:
            break
        time.sleep(0.05)
    else:
        pytest.fail("uvicorn did not start")

    yield cfg, store

    server.should_exit = True
    thread.join(timeout=10)
    store.close()


def test_the_documented_command_captures_a_realistic_prompt(live_server, tmp_path):
    cfg, store = live_server
    project = tmp_path / "a project with spaces"
    project.mkdir()
    # A summary is a natural place for quotes and shell metacharacters. Passed
    # as `--summary "<text>"` this was mangled, and `$(...)` would have executed.
    summary = 'Built Task 4; the "slash" command $(echo nope) `x` round-trips'

    script = (
        command_template()
        .replace("<your one-line summary>", summary)
        .replace(
            "<your next-session prompt, as many paragraphs as it needs>",
            REALISTIC_PROMPT,
        )
    )

    proc = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=str(project),
        capture_output=True,
        text=True,
        env={
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "BRIDGE_PORT": str(cfg.port),
            "CLAUDE_CODE_SESSION_ID": SESSION_ID,
            "CLAUDE_EFFORT": "high",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "queued for" in proc.stderr, proc.stderr

    row = store.queued_handoff(store.project_by_path(str(project))["id"])
    assert row is not None, "the handoff did not attach to the cwd's project"
    # A heredoc appends a newline after the body, so that single trailing byte is
    # expected and pinned here rather than glossed over.
    assert row["next_prompt"] == REALISTIC_PROMPT + "\n"
    assert row["summary"] == summary
    assert row["source_session_id"] == SESSION_ID
    assert row["suggested_effort"] == "high"
    # The quoted heredoc must have prevented every form of expansion.
    assert '$(echo "not expanded")' in row["next_prompt"]
    assert "`backticks`" in row["next_prompt"]
    assert "${BRACES}" in row["next_prompt"]


def test_the_command_file_documents_the_quoted_heredoc_and_stdin(live_server=None):
    """The two rules that keep a prompt from being executed or truncated."""
    md = (REPO / "commands" / "handoff.md").read_text()
    template = command_template()
    assert "<<'BRIDGE_PROMPT'" in template, "the heredoc delimiter must be quoted"
    assert "--prompt-file -" in template, "the prompt must arrive on stdin"
    assert "<<'BRIDGE_SUMMARY'" in template, (
        "the summary must go through a quoted heredoc too: interpolated into the "
        "command line, a summary containing $(...) or backticks is executed"
    )
    assert '--summary "$summary"' in template
    assert "spooled to" in md, "the command must state that a spool message is success"
