import os
import stat

import bridge.update as U


def test_token_created_0600_and_stable(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    t1 = U.read_or_create_token()
    assert len(t1) >= 32
    mode = stat.S_IMODE(os.stat(tmp_path / "token").st_mode)
    assert mode == 0o600
    assert U.read_or_create_token() == t1   # stable across calls


def test_token_meta_in_panel(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from bridge.api import create_app
    from bridge.config import load
    from bridge.store import Store
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    projects = tmp_path / "p"; projects.mkdir()
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "s",
                "claude_projects_dir": projects})
    store = Store(cfg.db_path)
    c = TestClient(create_app(store, cfg))
    html = c.get("/").text
    assert 'name="bridge-update-token"' in html
    store.close()
