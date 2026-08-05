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
    """A real HTTP server on a real port, with a settable status code.

    `/api/handoffs` (plural, by-path list) and `/api/handoff` (singular) are
    routed separately so a test can assert both that the right GET happened
    and what the POST body carried, rather than one stub answering for both.
    """
    state = {"code": 201, "get_body": None, "posts": [], "gets": [],
             "post_body": {"id": "server-side"},
             "handoffs_body": [{"id": "auto-handoff", "summary": "auto-picked"}]}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            state["posts"].append(json.loads(self.rfile.read(length) or b"{}"))
            self.send_response(state["code"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(state["post_body"]).encode())

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            state["gets"].append(path)
            if path == "/api/handoffs":
                # Contract: always 200 with a list, never 204, even when empty.
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(state["handoffs_body"]).encode())
                return
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


def test_launching_an_empty_prompt_file_launches_nothing(monkeypatch, tmp_path,
                                                         fake_server):
    """Passing `--prompt-file` is explicit intent, so an empty one cannot fall
    through to the queued handoff the way omitting the flag does. Posting the
    whitespace would spawn a session with no instructions instead."""
    fake_server["code"] = 200
    fake_server["post_body"] = started()
    code, _ = run_launch(
        monkeypatch, tmp_path, fake_server["port"],
        argv=["launch", "--project", DEMO, "--prompt-file", "-"],
        prompt="   \n",
    )
    assert code == 2
    assert fake_server["posts"] == [], "an empty prompt was launched anyway"


def test_launch_without_a_prompt_file_sends_no_prompt_at_all(monkeypatch, tmp_path,
                                                             fake_server):
    """Asserted as an absence, because the client auto-picks the one queued
    handoff by id rather than reading (or round-tripping) its prompt: an
    empty-string prompt would look like a request to launch nothing, and
    echoing the queued prompt back would let the client launch text the panel
    no longer holds.

    This is also the regression test for the bug this task fixes: with no
    `--prompt-file` and no `--handoff`, the CLI used to POST a payload with
    neither key, which now 422s server-side. The fix is client-side: GET the
    project's queued handoffs first and post the one that comes back.
    """
    fake_server["code"] = 200
    fake_server["post_body"] = started()
    fake_server["handoffs_body"] = [{"id": "only-queued", "summary": "s"}]
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"])
    assert code == 0
    assert "/api/handoffs" in fake_server["gets"]
    posted = fake_server["posts"][0]
    assert "prompt" not in posted, f"the CLI sent a prompt anyway: {posted}"
    assert posted["handoff_id"] == "only-queued"
    assert posted["mode"] == "terminal", "--mode defaults to terminal"


def test_launch_with_handoff_flag_posts_that_handoff_id_and_no_prompt(
    monkeypatch, tmp_path, fake_server
):
    """`--handoff <id>` is an explicit target, so it skips the GET entirely."""
    fake_server["code"] = 200
    fake_server["post_body"] = started()
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"], argv=[
        "launch", "--project", DEMO, "--handoff", "h-explicit",
    ])
    assert code == 0
    assert "/api/handoffs" not in fake_server["gets"]
    posted = fake_server["posts"][0]
    assert posted["handoff_id"] == "h-explicit"
    assert "prompt" not in posted


def test_launch_with_nothing_queued_client_side_exits_two_and_posts_nothing(
    monkeypatch, tmp_path, fake_server, capsys
):
    """The server no longer auto-picks and no longer answers this case at all
    (Task 3 made it a 422), so the client must catch an empty project before
    ever POSTing."""
    fake_server["handoffs_body"] = []
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"])
    out = capsys.readouterr()
    assert code == 2
    assert fake_server["posts"] == []
    assert "nothing queued" in out.err


def test_launch_with_multiple_queued_handoffs_lists_them_and_posts_nothing(
    monkeypatch, tmp_path, fake_server, capsys
):
    """Grabbing one at random would fire whichever happened to be newest
    instead of the one the caller meant, so a project with several queued
    handoffs must refuse and name them rather than guess."""
    fake_server["handoffs_body"] = [
        {"id": "h1", "summary": "first summary"},
        {"id": "h2", "summary": "second summary"},
    ]
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"])
    out = capsys.readouterr()
    assert code == 2
    assert fake_server["posts"] == []
    assert "h1" in out.err and "first summary" in out.err
    assert "h2" in out.err and "second summary" in out.err
    assert "--handoff" in out.err


def test_launch_with_both_prompt_file_and_handoff_is_a_usage_error(
    monkeypatch, tmp_path, fake_server, capsys
):
    """Two explicit targets that might disagree is a user error worth naming,
    caught before any HTTP is attempted."""
    code, _ = run_launch(
        monkeypatch, tmp_path, fake_server["port"],
        argv=["launch", "--project", DEMO, "--prompt-file", "-",
              "--handoff", "h1"],
        prompt=HOSTILE,
    )
    out = capsys.readouterr()
    assert code == 2
    assert "pick one" in out.err
    assert fake_server["posts"] == []
    assert fake_server["gets"] == []


@pytest.mark.parametrize("code_and_body", [
    (409, {"detail": "spawn refused"}),
    (200, {"outcome": "failed", "error": "spawn failed",
           "session_id": None, "launch_id": "l-1"}),
], ids=["refused-outright", "reported-as-a-failed-outcome"])
def test_launch_that_reaches_the_server_and_fails_exits_nonzero_and_says_why(
    monkeypatch, tmp_path, fake_server, capsys, code_and_body
):
    """Matching `bridge next`: a launch that does not start is a non-zero exit
    with a message. `--handoff` is passed explicitly so this exercises the
    generic POST-failure handling on its own, independent of target
    resolution (covered separately above).

    Both shapes the API can express this in are covered — a refusal, and the
    200-with-`outcome='failed'` the panel needs in order to show the error beside
    the prompt — because either way the CLI must fail and must repeat the
    server's own words rather than inventing a guess.
    """
    fake_server["code"], fake_server["post_body"] = code_and_body
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"], argv=[
        "launch", "--project", DEMO, "--handoff", "h1",
    ])
    out = capsys.readouterr()
    assert code == 1
    assert out.out == ""
    assert "spawn" in out.err


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


# --- Phase 4 Task 2: permission modes ----------------------------------------


def test_launch_posts_no_permission_mode_by_default(monkeypatch, tmp_path,
                                                    fake_server):
    """The default must be absent, not a benign-looking value."""
    fake_server["code"] = 200
    fake_server["post_body"] = started()
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"], argv=[
        "launch", "--project", DEMO,
    ])
    assert code == 0
    assert fake_server["posts"][0]["permission_mode"] is None


def test_launch_posts_the_requested_permission_mode(monkeypatch, tmp_path,
                                                    fake_server):
    fake_server["code"] = 200
    fake_server["post_body"] = started()
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"], argv=[
        "launch", "--project", DEMO, "--permission-mode", "plan",
    ])
    assert code == 0
    assert fake_server["posts"][0]["permission_mode"] == "plan"


def test_the_dangerous_alias_resolves_to_the_one_mode_it_means(monkeypatch,
                                                               tmp_path,
                                                               fake_server):
    """Spelled the way `claude` spells it so muscle memory transfers, but the
    wire carries the enum value -- there is no second code path for it."""
    fake_server["code"] = 200
    fake_server["post_body"] = started()
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"], argv=[
        "launch", "--project", DEMO, "--dangerously-skip-permissions",
    ])
    assert code == 0
    assert fake_server["posts"][0]["permission_mode"] == "bypassPermissions"


def test_asking_for_two_contradictory_permission_modes_launches_nothing(
        monkeypatch, tmp_path, fake_server):
    """Silently picking a winner would run a session under a mode the user did
    not unambiguously ask for. Refuse instead, before anything is posted."""
    fake_server["code"] = 200
    fake_server["post_body"] = started()
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"], argv=[
        "launch", "--project", DEMO, "--permission-mode", "plan",
        "--dangerously-skip-permissions",
    ])
    assert code == 2
    assert fake_server["posts"] == [], "a contradictory launch still posted"


def test_the_cli_refuses_a_mode_the_binary_does_not_accept(monkeypatch, tmp_path,
                                                           fake_server):
    """argparse rejects it locally, so `default` -- which belongs to
    settings.json's `permissions.defaultMode`, not to this flag -- cannot reach
    the server and fail the spawn."""
    code, _ = run_launch(monkeypatch, tmp_path, fake_server["port"], argv=[
        "launch", "--project", DEMO, "--permission-mode", "default",
    ])
    assert code != 0
    assert fake_server["posts"] == []
