import bridge.cli as cli
import bridge.update as U
from bridge.config import load


def test_version_shows_sha_and_method(monkeypatch, capsys):
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    monkeypatch.setattr(U, "install_method", lambda: "uv")
    rc = cli.main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "aaaaaaaaaaaa" in out       # 12-char short SHA
    assert "uv" in out


def test_update_success_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(U, "resolve_remote_sha", lambda **k: "b" * 40)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    monkeypatch.setattr(U, "run_update", lambda sha: U.UpdateResult(
        ok=True, previous_sha="a" * 40, attempted_sha=sha, method="uv",
        started_at="t", ended_at="t", exit_status=0, log_path="/l",
        error=None, rolled_back=False))
    rc = cli.main(["update"])
    assert rc == 0
    assert "updated" in capsys.readouterr().err.lower()


def test_update_failure_exits_one(monkeypatch, capsys):
    monkeypatch.setattr(U, "resolve_remote_sha", lambda **k: "b" * 40)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    monkeypatch.setattr(U, "run_update", lambda sha: U.UpdateResult(
        ok=False, previous_sha="a" * 40, attempted_sha=sha, method="brew",
        started_at="t", ended_at="t", exit_status=1, log_path="/l",
        error="installer exited 1", rolled_back=False))
    rc = cli.main(["update"])
    assert rc == 1
    assert "installer exited 1" in capsys.readouterr().err


def test_update_current_is_noop(monkeypatch, capsys):
    monkeypatch.setattr(U, "resolve_remote_sha", lambda **k: "a" * 40)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    rc = cli.main(["update"])
    assert rc == 0
    assert "up to date" in capsys.readouterr().err.lower()
