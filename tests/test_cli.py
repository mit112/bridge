"""Tests for the `bridge` CLI.

The headline property — `bridge handoff` exits zero when the panel is down — is
tested against a genuinely closed TCP port in a real subprocess. A mocked
transport would prove only that the mock raises what the test told it to.
"""

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from bridge import cli, spool
from bridge.config import load

DEMO = "/Users/mitsheth/dev/demo"
HOSTILE = (
    'quotes " backticks `whoami` $(echo pwned) ${HOME}\n'
    "newlines and a tab\there\némoji 🌉 <script>\n"
)


def closed_port() -> int:
    """A port with nothing listening, verified rather than assumed."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    probe = socket.socket()
    probe.settimeout(0.25)
    assert probe.connect_ex(("127.0.0.1", port)) != 0, f"port {port} is open"
    probe.close()
    return port


@pytest.fixture
def fake_server():
    """A real HTTP server on a real port, with a settable status code."""
    state = {"code": 201, "get_body": None, "posts": [],
             "post_body": {"id": "server-side"}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            state["posts"].append(json.loads(self.rfile.read(length) or b"{}"))
            self.send_response(state["code"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(state["post_body"]).encode())

        def do_GET(self):
            body = state["get_body"]
            self.send_response(200 if body is not None else 204)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if body is not None:
                self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["port"] = server.server_address[1]
    yield state
    server.shutdown()
    server.server_close()


def cfg_for(tmp_path, port):
    return load({
        "db_path": tmp_path / "cli.db",
        "spool_dir": tmp_path / "spool",
        "launches_dir": tmp_path / "launches",
        "port": port,
    })


def run_handoff(monkeypatch, tmp_path, port, prompt=HOSTILE, argv=None):
    cfg = cfg_for(tmp_path, port)
    monkeypatch.setattr(cli, "load", lambda overrides=None: cfg)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(prompt))
    code = cli.main(argv or [
        "handoff", "--summary", "a summary", "--prompt-file", "-",
        "--project", DEMO, "--session-id", "sess-1",
    ])
    return code, cfg


# --- the property the whole phase rests on -----------------------------------


def test_handoff_exits_zero_and_spools_against_a_genuinely_closed_port(tmp_path):
    """A session must never fail because the panel is down.

    Run as a real subprocess against a real closed port, with HOME relocated so
    the spool lands in tmp_path.
    """
    port = closed_port()
    home = tmp_path / "home"
    home.mkdir()
    prompt = HOSTILE + "padding " * 2000

    proc = subprocess.run(
        [sys.executable, "-m", "bridge", "handoff", "--summary", "s",
         "--prompt-file", "-", "--project", DEMO, "--session-id", "sess-1"],
        input=prompt, capture_output=True, text=True,
        cwd=str(tmp_path),
        env={"HOME": str(home), "PATH": "/usr/bin:/bin",
             "BRIDGE_PORT": str(port), "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert proc.returncode == 0, f"stderr:\n{proc.stderr}"
    spooled = list((home / ".bridge" / "spool").glob("*.json"))
    assert len(spooled) == 1, f"expected one spool file, got {spooled}"
    record = json.loads(spooled[0].read_text())
    assert record["next_prompt"] == prompt
    assert record["project_path"] == DEMO
    assert record["source_session_id"] == "sess-1"
    assert record["status"] == "queued"
    assert "spooled to" in proc.stderr


def test_a_500_from_the_server_also_spools_and_exits_zero(monkeypatch, tmp_path,
                                                          fake_server):
    """Catching only connection errors would drop the prompt on a 5xx."""
    fake_server["code"] = 500
    code, cfg = run_handoff(monkeypatch, tmp_path, fake_server["port"])
    assert code == 0
    assert spool.pending_count(cfg.spool_dir) == 1


def test_a_4xx_from_the_server_also_spools_and_exits_zero(monkeypatch, tmp_path,
                                                          fake_server):
    """A CLI/server disagreement is not the session's problem to absorb."""
    fake_server["code"] = 422
    code, cfg = run_handoff(monkeypatch, tmp_path, fake_server["port"])
    assert code == 0
    assert spool.pending_count(cfg.spool_dir) == 1


def test_a_2xx_does_not_spool(monkeypatch, tmp_path, fake_server):
    """An accepted handoff must not leave an outbox file behind for a drain to
    re-ingest; the server journals it instead."""
    fake_server["code"] = 201
    code, cfg = run_handoff(monkeypatch, tmp_path, fake_server["port"])
    assert code == 0
    assert spool.pending_count(cfg.spool_dir) == 0
    assert fake_server["posts"][0]["next_prompt"] == HOSTILE
    assert fake_server["posts"][0]["session_id"] == "sess-1"


def test_an_empty_prompt_is_a_usage_error_not_a_silent_success(monkeypatch, tmp_path):
    """Exiting zero here would report success for a handoff recording nothing."""
    code, cfg = run_handoff(monkeypatch, tmp_path, closed_port(), prompt="   \n")
    assert code == 2
    assert spool.pending_count(cfg.spool_dir) == 0


# --- bridge next -------------------------------------------------------------


def test_next_prints_exactly_the_prompt_and_nothing_else(monkeypatch, tmp_path,
                                                         fake_server, capsys):
    """`claude "$(bridge next)"` must not receive a banner or a log line."""
    prompt = HOSTILE + "carry on"
    fake_server["get_body"] = {"id": "h1", "next_prompt": prompt}
    cfg = cfg_for(tmp_path, fake_server["port"])
    monkeypatch.setattr(cli, "load", lambda overrides=None: cfg)

    code = cli.main(["next", "--project", DEMO])

    out = capsys.readouterr()
    assert code == 0
    assert out.out == prompt, "stdout must be the prompt byte for byte"


def test_next_with_nothing_queued_exits_nonzero_and_prints_nothing(
    monkeypatch, tmp_path, fake_server, capsys
):
    """Exit zero here and `claude "$(bridge next)"` opens an empty session."""
    fake_server["get_body"] = None  # 204
    cfg = cfg_for(tmp_path, fake_server["port"])
    monkeypatch.setattr(cli, "load", lambda overrides=None: cfg)

    code = cli.main(["next", "--project", DEMO])

    out = capsys.readouterr()
    assert code != 0
    assert out.out == ""
    assert "nothing queued" in out.err


def test_next_with_the_panel_down_exits_nonzero_and_prints_nothing(
    monkeypatch, tmp_path, capsys
):
    cfg = cfg_for(tmp_path, closed_port())
    monkeypatch.setattr(cli, "load", lambda overrides=None: cfg)
    code = cli.main(["next", "--project", DEMO])
    out = capsys.readouterr()
    assert code != 0
    assert out.out == ""


# --- Phase 3: the launcher ---------------------------------------------------


def run_launch(monkeypatch, tmp_path, port, argv=None, prompt=None):
    cfg = cfg_for(tmp_path, port)
    monkeypatch.setattr(cli, "load", lambda overrides=None: cfg)
    if prompt is not None:
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(prompt))
    code = cli.main(argv or ["launch", "--project", DEMO])
    return code, cfg


def started(**extra):
    """What `POST /api/launch` returns for a launch that started."""
    return {"outcome": "started", "session_id": "1234", "launch_id": "l-1", **extra}


def test_launch_exits_one_where_handoff_exits_zero_on_the_same_closed_port(tmp_path):
    """The two commands are deliberately asymmetric, so they are asserted together.

    Same closed port, same HOME, same subprocess machinery: the only difference is
    the subcommand. `handoff` must survive a dead panel because it is holding the
    only copy of something. `launch` must not, because a launch that did not
    happen has lost nothing and a launch deferred to some later boot is worse
    than one that never fired.
    """
    port = closed_port()
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "BRIDGE_PORT": str(port), "PYTHONDONTWRITEBYTECODE": "1"}

    def run(argv, stdin=""):
        return subprocess.run(
            [sys.executable, "-m", "bridge", *argv],
            input=stdin, capture_output=True, text=True,
            cwd=str(tmp_path), env=env,
        )

    launch = run(["launch", "--project", DEMO])
    handoff = run(["handoff", "--prompt-file", "-", "--project", DEMO], HOSTILE)

    assert (launch.returncode, handoff.returncode) == (1, 0), (
        "launch must fail loudly and handoff must not fail at all\n"
        f"launch stderr:\n{launch.stderr}\nhandoff stderr:\n{handoff.stderr}"
    )
    assert launch.stdout == ""
    assert "panel unreachable" in launch.stderr


def test_a_failed_launch_writes_no_spool_file(tmp_path):
    """A spooled launch would fire at an unpredictable later time. Nothing at all
    must be left behind that a drain could pick up."""
    port = closed_port()
    home = tmp_path / "home"
    home.mkdir()

    proc = subprocess.run(
        [sys.executable, "-m", "bridge", "launch", "--project", DEMO],
        input="", capture_output=True, text=True, cwd=str(tmp_path),
        env={"HOME": str(home), "PATH": "/usr/bin:/bin",
             "BRIDGE_PORT": str(port), "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert proc.returncode == 1, f"stderr:\n{proc.stderr}"
    left = [p for p in (home / ".bridge").rglob("*") if p.is_file()]
    assert left == [], f"a failed launch left files behind: {left}"


def test_launch_posts_the_project_mode_model_and_effort(monkeypatch, tmp_path,
                                                        fake_server):
    fake_server["code"] = 200
    fake_server["post_body"] = started()
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"], argv=[
        "launch", "--project", DEMO, "--mode", "background",
        "--model", "opus", "--effort", "xhigh",
    ])
    assert code == 0
    posted = fake_server["posts"][0]
    assert posted["project_path"] == DEMO
    assert posted["mode"] == "background"
    assert posted["model"] == "opus"
    assert posted["effort"] == "xhigh"


def test_launch_with_a_prompt_file_overrides_the_queued_handoff(monkeypatch, tmp_path,
                                                                fake_server):
    """`--prompt-file -` is the escape hatch for launching something other than
    whatever the panel has queued."""
    fake_server["code"] = 200
    fake_server["post_body"] = started()
    code, _ = run_launch(
        monkeypatch, tmp_path, fake_server["port"],
        argv=["launch", "--project", DEMO, "--prompt-file", "-"],
        prompt=HOSTILE,
    )
    assert code == 0
    assert fake_server["posts"][0]["prompt"] == HOSTILE


def test_launch_without_a_prompt_file_sends_no_prompt_at_all(monkeypatch, tmp_path,
                                                             fake_server):
    """Asserted as an absence, because the server-side fallback to the queued
    handoff is the whole point: an empty-string prompt would look like a request
    to launch nothing, and echoing the queued prompt back would let the client
    launch text the panel no longer holds."""
    fake_server["code"] = 200
    fake_server["post_body"] = started()
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"])
    assert code == 0
    posted = fake_server["posts"][0]
    assert "prompt" not in posted, f"the CLI sent a prompt anyway: {posted}"
    assert posted["mode"] == "terminal", "--mode defaults to terminal"


@pytest.mark.parametrize("code_and_body", [
    (409, {"detail": "nothing queued for " + DEMO}),
    (200, {"outcome": "failed", "error": "nothing queued for " + DEMO,
           "session_id": None, "launch_id": "l-1"}),
], ids=["refused-outright", "reported-as-a-failed-outcome"])
def test_launch_with_nothing_queued_exits_nonzero_and_says_why(
    monkeypatch, tmp_path, fake_server, capsys, code_and_body
):
    """Matching `bridge next`: nothing to run is a non-zero exit with a message.

    Both shapes the API can express this in are covered — a refusal, and the
    200-with-`outcome='failed'` the panel needs in order to show the error beside
    the prompt — because either way the CLI must fail and must repeat the
    server's own words rather than inventing a guess.
    """
    fake_server["code"], fake_server["post_body"] = code_and_body
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"])
    out = capsys.readouterr()
    assert code == 1
    assert out.out == ""
    assert "nothing queued" in out.err


# --- structure ---------------------------------------------------------------


def test_the_cli_never_loads_a_database_module():
    """Asserted by observation, not by reading imports: the CLI must not reach
    the database even transitively. Deterministic, unlike a timing test.

    `bridge.launcher` is held to the same rule from Phase 3 on. The server is the
    sole spawner as well as the sole writer, so importing the launcher here would
    both slow the end-of-session path down and invite a future `bridge launch`
    that spawns client-side, leaving no `launches` row behind it.
    """
    probe = (
        "import bridge.cli, sys;"
        "print('sqlite3' in sys.modules, 'bridge.store' in sys.modules,"
        " 'bridge.launcher' in sys.modules)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
             "HOME": str(Path.home())},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False False False", (
        f"the CLI loaded a database or launcher module: {proc.stdout.strip()}"
    )
