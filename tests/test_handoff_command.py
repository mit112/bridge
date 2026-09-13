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


def bound_port() -> tuple[socket.socket, int]:
    """A listening socket and its port, handed to uvicorn as-is.

    Asking the kernel for a free port, closing it, and letting uvicorn bind the
    same number a moment later is a TOCTOU race: anything else on the machine
    can take the port in the gap and the fixture fails for a reason that has
    nothing to do with Bridge. Keeping the socket open means the port cannot be
    taken, because we never let go of it.
    """
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(128)
    return s, s.getsockname()[1]


def command_template() -> str:
    """The bash block in the command file that invokes `bridge handoff`.

    Located by content, not by position. The file grew a `## 0. Probe` block of
    read-only `git` commands ahead of the invocation, and "the first bash block"
    silently became the wrong one -- a locator that keeps finding *a* block is
    exactly how this suite would go on passing while testing nothing. Requiring
    exactly one match is what keeps that failure loud if a second invocation is
    ever documented.
    """
    md = (REPO / "commands" / "handoff.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)```", md, re.S)
    assert blocks, "commands/handoff.md has no bash block"
    invocations = [b for b in blocks if "bridge handoff" in b]
    assert len(invocations) == 1, (
        f"expected exactly one `bridge handoff` bash block, found "
        f"{len(invocations)} among {len(blocks)} blocks"
    )
    return invocations[0]


@pytest.fixture
def live_server(tmp_path):
    """A real server on a real port, not a TestClient transport."""
    import uvicorn

    sock, port = bound_port()
    cfg = load({
        "db_path": tmp_path / "live.db",
        "spool_dir": tmp_path / "spool",
        "port": port,
    })
    store = Store(cfg.db_path)
    server = uvicorn.Server(
        uvicorn.Config(create_app(store, cfg), host="127.0.0.1",
                       port=cfg.port, log_level="error")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                              daemon=True)
    thread.start()

    # The socket is already listening, so a connect() probe would succeed
    # before uvicorn has accepted anything. `started` is the flag uvicorn sets
    # once it is actually serving on it.
    deadline = time.time() + 15
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        pytest.fail("uvicorn did not start")

    yield cfg, store

    server.should_exit = True
    thread.join(timeout=10)
    sock.close()
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


def test_the_documented_command_closes_the_thread_it_was_launched_from(
    live_server, tmp_path
):
    """The `${closes:+…}` branch, taken.

    The realistic-prompt test above always runs with an EMPTY `closes`, because
    nothing launched it -- so it proves the fragment collapses to nothing and
    says exactly nothing about the case the fragment exists for. Here a launch
    row makes `bridge origin` answer, and the same documented block has to come
    out the other side with a thread recorded.
    """
    from bridge.models import Handoff, Launch

    cfg, store = live_server
    project = tmp_path / "threaded"
    project.mkdir()
    pid = store.upsert_project(str(project), "threaded")
    store.create_handoff(
        Handoff(id="parent-handoff", project_path=str(project),
                next_prompt="the previous plan", summary="Built Task 4",
                created_at=1000),
        pid,
    )
    store.create_launch(Launch(id="l1", project_id=pid,
                               handoff_id="parent-handoff",
                               session_id=SESSION_ID, mode="terminal",
                               prompt="the previous plan", launched_at=1001))

    script = (
        command_template()
        .replace("<your one-line summary>", "Finished Task 4")
        .replace("<your next-session prompt, as many paragraphs as it needs>",
                 "Start Task 5.")
    )
    proc = subprocess.run(
        ["/bin/bash", "-c", script], cwd=str(project),
        capture_output=True, text=True,
        env={
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "HOME": str(tmp_path / "home2"),
            "BRIDGE_PORT": str(cfg.port),
            "CLAUDE_CODE_SESSION_ID": SESSION_ID,
            "CLAUDE_EFFORT": "high",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    written = [h for h in store.handoffs(pid) if h["id"] != "parent-handoff"]
    assert len(written) == 1, "the documented command wrote no new handoff"
    assert written[0]["parent_handoff_id"] == "parent-handoff"
    assert written[0]["parent_outcome"] == "done"


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
    assert "${closes:+" in template, (
        "the thread flags must stay conditional: a session nobody launched has "
        "no origin, and `--closes ''` would post a link to nothing"
    )
    assert "bridge origin" in md, "the command must say where `closes` comes from"
