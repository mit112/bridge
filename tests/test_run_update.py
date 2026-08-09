import bridge.update as U


def _stub(monkeypatch, tmp_path, method="uv", verify=True, installer_exit=0):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "install_method", lambda: method)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    calls = []
    def fake_installer(cmd, env, log_path):
        calls.append(cmd)
        return installer_exit
    monkeypatch.setattr(U, "_run_installer", fake_installer)
    monkeypatch.setattr(U, "_verify_fresh_pid", lambda sha: verify)
    return calls


def test_run_update_uv_success(monkeypatch, tmp_path):
    calls = _stub(monkeypatch, tmp_path)
    res = U.run_update("b" * 40)
    assert res.ok is True
    assert res.rolled_back is False
    assert res.attempted_sha == "b" * 40
    assert res.previous_sha == "a" * 40
    assert res.method == "uv"
    assert calls[0][:3] == ["uv", "tool", "install"]
    assert f"git+{U.REPO_URL[:-4]}@{'b' * 40}" in " ".join(calls[0])


def test_run_update_rolls_back_on_mismatch(monkeypatch, tmp_path):
    calls = _stub(monkeypatch, tmp_path, verify=False)
    res = U.run_update("b" * 40)
    assert res.ok is False
    assert res.rolled_back is True          # uv rollback reinstalls previous SHA
    assert calls[-1][:3] == ["uv", "tool", "install"]
    assert "a" * 40 in " ".join(calls[-1])  # reinstalled the previous SHA


def test_run_update_brew_prints_recovery_no_rollback(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, method="brew", verify=False)
    res = U.run_update("b" * 40)
    assert res.ok is False
    assert res.rolled_back is False         # brew rollback unsupported
    assert "brew" in (res.error or "").lower()


def test_run_update_refuses_dev(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, method="dev")
    res = U.run_update("b" * 40)
    assert res.ok is False
    assert "dev" in (res.error or "").lower() or "unknown" in (res.error or "").lower()


def test_run_update_rejects_concurrent(monkeypatch, tmp_path):
    calls = _stub(monkeypatch, tmp_path)
    fd = U._acquire_lock()
    try:
        res = U.run_update("b" * 40)
        assert res.ok is False
        assert "in progress" in (res.error or "").lower()
        assert calls == []          # installer must never run on the held-lock path
    finally:
        U._release_lock(fd)


def test_run_update_uv_no_previous_sha_message_not_brew(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "install_method", lambda: "uv")
    monkeypatch.setattr(U, "installed_sha", lambda: None)
    calls = []
    def fake_installer(cmd, env, log_path):
        calls.append(cmd)
        return 0
    monkeypatch.setattr(U, "_run_installer", fake_installer)
    monkeypatch.setattr(U, "_verify_fresh_pid", lambda sha: False)
    res = U.run_update("b" * 40)
    assert res.ok is False
    assert res.rolled_back is False
    assert res.previous_sha is None
    assert "brew" not in (res.error or "").lower()
    assert "uv" in (res.error or "").lower()
    assert len(calls) == 1  # no rollback attempted -- nothing to roll back to


def test_run_update_returns_failed_result_when_installer_binary_is_absent(
        monkeypatch, tmp_path):
    """`uv`/`brew` absent makes `subprocess.run` raise FileNotFoundError (an
    OSError). run_update is the transaction boundary: it must catch it and
    return ok=False, never let it propagate -- or the launchagent flow skips its
    reconnect-state write and the endpoint 500s instead of returning JSON."""
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "install_method", lambda: "uv")
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)

    def boom(cmd, env, log_path):
        raise FileNotFoundError(2, "No such file or directory", "uv")

    monkeypatch.setattr(U, "_run_installer", boom)
    res = U.run_update("b" * 40)  # must NOT raise
    assert res.ok is False
    assert res.error and "uv" in res.error
    # The lock must have been released -- a second attempt is not refused.
    res2 = U.run_update("b" * 40)
    assert res2.ok is False


def test_run_update_uv_rollback_failure_message(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "install_method", lambda: "uv")
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    calls = []
    def fake_installer(cmd, env, log_path):
        calls.append(cmd)
        # first call installs the target (succeeds); second call is the
        # rollback reinstall of the previous SHA, which fails.
        return 0 if len(calls) == 1 else 7
    monkeypatch.setattr(U, "_run_installer", fake_installer)
    monkeypatch.setattr(U, "_verify_fresh_pid", lambda sha: False)
    res = U.run_update("b" * 40)
    assert res.ok is False
    assert res.rolled_back is False        # the rollback attempt itself failed
    assert "7" in (res.error or "")
    assert "fail" in (res.error or "").lower()
