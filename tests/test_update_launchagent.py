"""The one-click self-update runner: a one-shot LaunchAgent updater job, the
panel-plist re-bootstrap after the interpreter moves, and the reconnect-state
file the panel reads to tell "updating…" from "crashed after update".

Every test here is hermetic: `generate_updater_plist` is a pure string
function, and `run_update_via_launchagent`'s install / re-bootstrap steps are
monkeypatched out so no real `launchctl` runs and no real install path moves.
The state file routes through `_update_dir()`, which conftest redirects into a
temp dir, so nothing lands in the real `~/.bridge/update/`.
"""

import plistlib

import bridge.setup as S


def test_updater_plist_is_one_shot_with_distinct_label():
    xml = S.generate_updater_plist("/usr/bin/python3", "b" * 40)
    data = plistlib.loads(xml.encode())
    assert data["Label"] == "dev.bridge.updater"
    assert data["Label"] != S.LAUNCHD_LABEL      # not a child of the panel job
    assert data["RunAtLoad"] is True
    assert "KeepAlive" not in data               # one-shot: must not respawn
    argv = data["ProgramArguments"]
    assert "update" in argv and ("b" * 40) in " ".join(argv)


import bridge.update as U


def test_via_launchagent_writes_reconnect_state(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "run_update", lambda sha: U.UpdateResult(
        ok=True, previous_sha="a" * 40, attempted_sha=sha, method="uv",
        started_at="t", ended_at="t", exit_status=0, log_path="/l",
        error=None, rolled_back=False))
    monkeypatch.setattr(U, "_rebootstrap_panel", lambda: True)
    res = U.run_update_via_launchagent("b" * 40)
    assert res.ok is True
    st = U.read_update_state()
    assert st.installed_sha == "b" * 40 or st.latest_sha == "b" * 40


def test_via_launchagent_writes_state_even_when_rebootstrap_raises(monkeypatch, tmp_path):
    """The interpreter-move re-bootstrap can raise -- SystemExit is a
    BaseException, so a bare `except Exception` would let it escape and skip the
    state write entirely. The reconnect-state file is the ONLY way the banner
    tells "updated" from "crashed" across the forced SSE reconnect, so it must
    be written on both a clean restart and a raised one, and the failure must
    surface in `UpdateResult.error` rather than vanishing."""
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "run_update", lambda sha: U.UpdateResult(
        ok=True, previous_sha="a" * 40, attempted_sha=sha, method="uv",
        started_at="t", ended_at="t", exit_status=0, log_path="/l",
        error=None, rolled_back=False))

    def boom():
        raise SystemExit(1)

    monkeypatch.setattr(U, "_rebootstrap_panel", boom)
    res = U.run_update_via_launchagent("b" * 40)
    st = U.read_update_state()
    assert st is not None
    assert st.installed_sha == "b" * 40
    assert res.error and "re-bootstrap" in res.error


def test_rebootstrap_panel_calls_launchd_only_non_interactively(monkeypatch):
    """The one-shot updater runs headless: `run_launchd_only` must not reach an
    interactive `_ask_yn` (which `sys.exit()`s on EOF in a launchd job), so the
    re-bootstrap passes `assume_yes=True` and never prompts."""
    calls = {}

    def fake_run_launchd_only(assume_yes=False):
        calls["assume_yes"] = assume_yes
        return 0

    monkeypatch.setattr(S, "run_launchd_only", fake_run_launchd_only)

    def no_prompt(*a, **k):
        raise AssertionError("_ask_yn must not be called in the headless path")

    monkeypatch.setattr(S, "_ask_yn", no_prompt)
    assert U._rebootstrap_panel() is True
    assert calls["assume_yes"] is True


def test_updater_plist_invokes_the_via_launchagent_runner():
    """The one-shot plist must run the panel-side flow (install + re-bootstrap +
    reconnect-state), NOT plain in-process `run_update` -- so its argv carries
    the dedicated `--via-launchagent` runner mode and the pinned `--sha`."""
    xml = S.generate_updater_plist("/usr/bin/python3", "b" * 40)
    data = plistlib.loads(xml.encode())
    argv = data["ProgramArguments"]
    assert "update" in argv
    assert "--via-launchagent" in argv
    assert "--sha" in argv
    assert ("b" * 40) in argv


def test_is_managed_launchagent_true_when_uv_and_panel_plist_present(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "install_method", lambda: "uv")
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / S.LAUNCHD_PLIST_NAME).write_text("<plist/>")
    monkeypatch.setattr(S, "LAUNCHD_AGENTS_DIR", agents)
    assert U.is_managed_launchagent() is True


def test_is_managed_launchagent_false_for_a_dev_install(monkeypatch):
    monkeypatch.setattr(U, "install_method", lambda: "dev")
    assert U.is_managed_launchagent() is False


def test_is_managed_launchagent_false_when_panel_plist_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "install_method", lambda: "uv")
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    monkeypatch.setattr(S, "LAUNCHD_AGENTS_DIR", agents)
    assert U.is_managed_launchagent() is False
