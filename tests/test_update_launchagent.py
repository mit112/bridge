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
