"""`/api/diagnostics` carries the `update` object the panel polls.

No new polling channel -- the endpoint already exists and is already polled;
this only asserts the `update` key rides along on the same body."""

from fastapi.testclient import TestClient

from bridge.api import create_app
from bridge.config import load
from bridge.store import Store
from bridge.update import UpdateChecker, UpdateState


def _client(tmp_path, checker):
    projects = tmp_path / "p"
    projects.mkdir()
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "s",
                "claude_projects_dir": projects})
    store = Store(cfg.db_path)
    return TestClient(create_app(store, cfg, update_checker=checker)), store


def test_diagnostics_carries_update_object(tmp_path, monkeypatch):
    ck = UpdateChecker(enabled=False)
    monkeypatch.setattr(ck, "snapshot", lambda: UpdateState(
        state="behind", installed_sha="a" * 40, latest_sha="b" * 40,
        checked_at="2026-08-08T00:00:00+00:00", error=None))
    c, store = _client(tmp_path, ck)
    body = c.get("/api/diagnostics").json()
    assert body["update"] == {
        "state": "behind", "installed_sha": "a" * 40, "latest_sha": "b" * 40,
        "checked_at": "2026-08-08T00:00:00+00:00", "error": None}
    store.close()


def test_diagnostics_update_defaults_unknown(tmp_path):
    # No checker passed -> a disabled checker -> unknown, never a network call.
    c, store = _client(tmp_path, None)
    body = c.get("/api/diagnostics").json()
    assert body["update"]["state"] == "unknown"
    store.close()
