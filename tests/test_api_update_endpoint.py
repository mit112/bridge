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
