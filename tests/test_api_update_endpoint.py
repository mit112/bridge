"""`POST /api/update` -- the CSRF-guarded one-click self-update trigger.

Three guards stack in front of `update.run_update`: a per-install bearer
token (Task 9), `Sec-Fetch-Site`, and an exact-SHA match against what the
checker currently surfaces as `behind`. The existing `_same_origin_writes_only`
middleware already enforces the `Origin` check on every unsafe method -- this
route adds the rest on top of it, not instead of it.
"""

from fastapi.testclient import TestClient

import bridge.update as U
from bridge.api import create_app
from bridge.config import load
from bridge.store import Store


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path / "u")
    (tmp_path / "u").mkdir()
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    projects = tmp_path / "p"; projects.mkdir()
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "s",
                "claude_projects_dir": projects})
    store = Store(cfg.db_path)
    ck = U.UpdateChecker(enabled=False)
    monkeypatch.setattr(ck, "snapshot", lambda: U.UpdateState(
        state="behind", installed_sha="a" * 40, latest_sha="b" * 40,
        checked_at="t", error=None))
    # Default to the UNMANAGED (manual `bridge serve`) path so the in-process
    # `run_update` behaviour every guard/accept test below asserts is
    # deterministic -- otherwise this would read the developer's own real
    # install method and real ~/Library/LaunchAgents panel plist. The managed
    # tests flip this seam back to True explicitly.
    monkeypatch.setattr(U, "is_managed_launchagent", lambda: False)
    app = create_app(store, cfg, update_checker=ck)
    return TestClient(app), store, U.read_or_create_token()


def test_rejects_missing_token(tmp_path, monkeypatch):
    c, store, _ = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 403
    store.close()


def test_rejects_empty_token(tmp_path, monkeypatch):
    # Forward-note from the Task 9 review: a stub used in isolated template
    # tests defaults the injected token to "". This must NOT be treated as a
    # valid credential by the real endpoint -- an explicit empty bearer value
    # is refused exactly like a missing header.
    c, store, _ = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": "Bearer ",
                        "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 403
    store.close()


def test_rejects_wrong_token(tmp_path, monkeypatch):
    c, store, _ = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": "Bearer WRONG",
                        "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 403
    store.close()


def test_rejects_cross_site(tmp_path, monkeypatch):
    c, store, tok = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    store.close()


def test_rejects_same_site(tmp_path, monkeypatch):
    # Only "same-origin" and "none" are accepted -- "same-site" is a browser
    # value too, and a browser only sends it for a cross-origin (even if
    # related-site) request, so it must be refused just like "cross-site".
    c, store, tok = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403
    store.close()


def test_rejects_cross_origin_header(tmp_path, monkeypatch):
    # The existing Origin middleware fires before the route body.
    c, store, tok = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "same-origin",
                        "Origin": "http://evil.example:8787",
                        "Host": "127.0.0.1"})
    assert r.status_code == 403
    store.close()


def test_rejects_sha_not_surfaced(tmp_path, monkeypatch):
    c, store, tok = _client(tmp_path, monkeypatch)
    r = c.post("/api/update", json={"target_sha": "c" * 40},  # not latest_sha
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 409
    store.close()


def test_accepts_valid_request(tmp_path, monkeypatch):
    c, store, tok = _client(tmp_path, monkeypatch)
    calls = []

    def fake_run_update(sha):
        calls.append(sha)
        return U.UpdateResult(
            ok=True, previous_sha="a" * 40, attempted_sha=sha, method="uv",
            started_at="t", ended_at="t", exit_status=0, log_path="/l",
            error=None, rolled_back=False)

    monkeypatch.setattr(U, "run_update", fake_run_update)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["attempted_sha"] == "b" * 40
    assert calls == ["b" * 40]
    store.close()


def test_managed_launchagent_installs_async_and_returns_accepted(tmp_path, monkeypatch):
    """Under a managed panel LaunchAgent the endpoint must NOT run in-process
    `run_update` (which reinstalls the package but leaves THIS panel process on
    the old code): it spawns the detached one-shot updater via
    `bootstrap_updater` and answers 202 immediately, letting the banner's
    reconnect read the update-state file once the panel restarts."""
    import bridge.setup as S

    c, store, tok = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(U, "is_managed_launchagent", lambda: True)
    boot_calls = []
    monkeypatch.setattr(S, "bootstrap_updater",
                        lambda sha: (boot_calls.append(sha), True)[1])

    def must_not_run(sha):
        raise AssertionError("managed path must not run in-process run_update")

    monkeypatch.setattr(U, "run_update", must_not_run)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 202
    assert r.json()["accepted"] is True
    assert boot_calls == ["b" * 40]
    store.close()


def test_managed_launchagent_bootstrap_failure_reports_an_error(tmp_path, monkeypatch):
    """A detached updater that fails to bootstrap must surface as an error the
    banner can show, not a silent 202 that leaves the user waiting forever."""
    import bridge.setup as S

    c, store, tok = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(U, "is_managed_launchagent", lambda: True)
    monkeypatch.setattr(S, "bootstrap_updater", lambda sha: False)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 500
    assert r.json()["ok"] is False
    store.close()


def test_unmanaged_serve_keeps_the_in_process_run_update(tmp_path, monkeypatch):
    """A manual `bridge serve` has no LaunchAgent to relaunch it, so it must
    keep the synchronous in-process install and return the UpdateResult JSON."""
    c, store, tok = _client(tmp_path, monkeypatch)  # is_managed defaulted False
    calls = []
    monkeypatch.setattr(U, "run_update", lambda sha: (calls.append(sha), U.UpdateResult(
        ok=True, previous_sha="a" * 40, attempted_sha=sha, method="uv",
        started_at="t", ended_at="t", exit_status=0, log_path="/l",
        error=None, rolled_back=False))[1])
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert calls == ["b" * 40]
    store.close()


def test_unmanaged_installer_binary_absent_returns_ok_false_not_500(
        tmp_path, monkeypatch):
    """`run_update` is the transaction boundary: if `uv`/`brew` is absent the
    installer raises FileNotFoundError, which must surface as a clean
    UpdateResult JSON with ok=false -- not escape the route as a 500 traceback.
    (TestClient re-raises server exceptions, so a leaked error fails this test.)"""
    c, store, tok = _client(tmp_path, monkeypatch)  # unmanaged by default
    monkeypatch.setattr(U, "install_method", lambda: "uv")

    def boom(cmd, env, log_path):
        raise FileNotFoundError(2, "No such file or directory", "uv")

    monkeypatch.setattr(U, "_run_installer", boom)
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}",
                        "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    store.close()


def test_accepts_when_sec_fetch_site_absent(tmp_path, monkeypatch):
    # A server-side client (CLI, curl) sends no Sec-Fetch-Site at all -- that
    # must stay allowed, same as the existing Origin middleware's contract.
    c, store, tok = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(U, "run_update", lambda sha: U.UpdateResult(
        ok=True, previous_sha="a" * 40, attempted_sha=sha, method="uv",
        started_at="t", ended_at="t", exit_status=0, log_path="/l",
        error=None, rolled_back=False))
    r = c.post("/api/update", json={"target_sha": "b" * 40},
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    store.close()
