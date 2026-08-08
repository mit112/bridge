"""Tests for the diagnosability lane: version exposure, `bridge diagnose`, and
logging configuration.

The diagnose helpers all take injectable seams (`run`, `serve_log`) so the suite
never shells out to the real `launchctl` and never reads the real
`~/.bridge/serve.log` -- both would make the result depend on the machine.
"""

import logging
import subprocess
import types

import pytest

from bridge import __version__, cli, configure_logging, diagnose
from bridge.config import load

DEMO = "/Users/you/dev/demo"


def cfg_for(tmp_path, port=8787):
    return load({
        "db_path": tmp_path / "cli.db",
        "spool_dir": tmp_path / "spool",
        "launches_dir": tmp_path / "launches",
        "port": port,
    })


def _completed(stdout: str, returncode: int = 0):
    """A stand-in for `subprocess.run(..., capture_output=True, text=True)`."""
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


# --- A: version exposure -----------------------------------------------------


def test_version_is_a_nonempty_string():
    assert isinstance(__version__, str) and __version__


def test_version_flag_prints_the_version_and_exits_zero(capsys):
    code = cli.main(["--version"])
    out = capsys.readouterr()
    assert code == 0
    assert __version__ in out.out


def test_status_reports_the_bridge_version(monkeypatch, tmp_path, capsys):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cfg = cfg_for(tmp_path, port)
    monkeypatch.setattr(cli, "load", lambda overrides=None: cfg)
    code = cli.main(["status", "--project", DEMO])
    out = capsys.readouterr()
    assert code == 0
    assert __version__ in out.out


# --- B: launch agent state ---------------------------------------------------


def test_launch_agent_state_reports_loaded_with_pid_when_listed():
    def run(cmd, **kw):
        return _completed("42\t0\tdev.bridge.panel\n-\t0\tcom.apple.other\n")

    state = diagnose.launch_agent_state(run=run)
    assert "loaded" in state
    assert "42" in state


def test_launch_agent_state_reports_not_loaded_when_absent_from_list():
    def run(cmd, **kw):
        return _completed("-\t0\tcom.apple.other\n123\t0\tcom.example.thing\n")

    assert diagnose.launch_agent_state(run=run) == "not loaded"


def test_launch_agent_state_handles_launchctl_missing():
    def run(cmd, **kw):
        raise FileNotFoundError("launchctl")

    assert "unavailable" in diagnose.launch_agent_state(run=run)


def test_launch_agent_state_handles_launchctl_nonzero_exit():
    def run(cmd, **kw):
        return _completed("", returncode=1)

    assert "unavailable" in diagnose.launch_agent_state(run=run)


# --- B: serve log tail -------------------------------------------------------


def test_log_tail_returns_the_last_n_lines(tmp_path):
    log = tmp_path / "serve.log"
    log.write_text("".join(f"line {i}\n" for i in range(100)))
    tail = diagnose.log_tail(log, n=5)
    assert "line 99" in tail
    assert "line 0" not in tail
    assert tail.count("\n") <= 5


def test_log_tail_handles_a_missing_log(tmp_path):
    tail = diagnose.log_tail(tmp_path / "nope.log", n=5)
    assert "no serve log" in tail.lower() or "no such" in tail.lower()


# --- B: full render ----------------------------------------------------------


def test_render_reports_version_python_and_platform(tmp_path):
    out = diagnose.render(
        cfg_for(tmp_path),
        serve_log=tmp_path / "serve.log",
        run=lambda cmd, **kw: _completed(""),
    )
    assert __version__ in out
    assert "python" in out.lower()
    assert "platform" in out.lower()


def test_render_reports_config_paths_and_port(tmp_path):
    cfg = cfg_for(tmp_path, port=9999)
    out = diagnose.render(
        cfg, serve_log=tmp_path / "serve.log",
        run=lambda cmd, **kw: _completed(""),
    )
    assert "9999" in out
    assert str(cfg.claude_projects_dir) in out


def test_render_includes_the_launch_agent_and_log_tail(tmp_path):
    log = tmp_path / "serve.log"
    log.write_text("[Errno 48] address already in use\n" * 3)
    out = diagnose.render(
        cfg_for(tmp_path), serve_log=log, log_lines=2,
        run=lambda cmd, **kw: _completed("42\t0\tdev.bridge.panel\n"),
    )
    assert "Errno 48" in out
    assert "42" in out


def test_diagnose_command_exits_zero(monkeypatch, tmp_path, capsys):
    cfg = cfg_for(tmp_path)
    monkeypatch.setattr(cli, "load", lambda overrides=None: cfg)
    monkeypatch.setattr(diagnose, "subprocess", subprocess)
    # Point the serve-log read at a hermetic, absent path via a tmp HOME.
    monkeypatch.setenv("HOME", str(tmp_path))
    code = cli.main(["diagnose"])
    out = capsys.readouterr()
    assert code == 0
    assert __version__ in out.out


# --- C: logging configuration ------------------------------------------------


def test_configure_logging_installs_a_root_handler_with_a_format():
    root = logging.getLogger()
    saved = root.handlers[:]
    saved_level = root.level
    try:
        for h in saved:
            root.removeHandler(h)
        configure_logging()
        assert root.handlers, "configure_logging installed no handler"
        fmt = root.handlers[0].formatter._fmt
        assert "asctime" in fmt
        assert "levelname" in fmt
        assert "name" in fmt or "module" in fmt
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)
        root.setLevel(saved_level)


def test_configure_logging_is_idempotent():
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        for h in saved:
            root.removeHandler(h)
        configure_logging()
        n = len(root.handlers)
        configure_logging()
        assert len(root.handlers) == n
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)
