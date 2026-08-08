"""Guards the wiring in `bridge.__main__`'s serve path.

The invariant that matters: the `ChangeNotifier` passed to `create_app` must be
the exact same instance whose `.bump` is the `RefreshCoordinator`'s
`on_change`. Otherwise `/api/refresh` and the file watcher never wake the SSE
stream `create_app` serves -- they'd be bumping a notifier nobody is waiting
on. These tests build the app the way `__main__.py` does in production
(notifier constructed once, injected into both the coordinator and
`create_app`) rather than the way most other route tests do (no coordinator,
no notifier), to catch a regression in that specific chain.
"""

from fastapi.testclient import TestClient

from bridge.config import load
from bridge.indexer import IndexStats
from bridge.notify import ChangeNotifier
from bridge.refresh import RefreshCoordinator
from bridge.store import Store
from bridge.api import create_app

DEMO = "/Users/mitsheth/dev/demo"


def _tmp_store_cfg(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    return store, cfg


def test_coordinator_on_change_bumps_the_injected_notifier(tmp_path):
    store, cfg = _tmp_store_cfg(tmp_path)
    n = ChangeNotifier()
    coord = RefreshCoordinator(
        store, cfg, reindex_fn=lambda s, c: IndexStats(), on_change=n.bump
    )
    before = n.revision
    coord.run_once()
    assert n.revision > before, "a reindex must wake the notifier through on_change"
    store.close()


def test_api_refresh_wakes_the_notifier_when_wired_like_production(tmp_path):
    # Builds the app exactly as `bridge.__main__` does: one notifier, injected
    # into the coordinator's `on_change` AND into `create_app`. This is the
    # regression guard for the "/api/refresh doesn't wake the SSE stream" gap
    # the Task 4 review flagged -- a test built with two separate notifiers
    # (or none at all) would pass while that gap was live.
    store, cfg = _tmp_store_cfg(tmp_path)
    notifier = ChangeNotifier()
    coord = RefreshCoordinator(
        store, cfg, reindex_fn=lambda s, c: IndexStats(), on_change=notifier.bump
    )
    app = create_app(store, cfg, refresh_coordinator=coord, notifier=notifier)
    client = TestClient(app)

    before = notifier.revision
    r = client.post("/api/refresh")
    assert r.status_code == 200
    assert notifier.revision > before, "/api/refresh must wake the injected notifier"
    store.close()


def test_patch_schedule_bumps_the_notifier(tmp_path):
    store, cfg = _tmp_store_cfg(tmp_path)
    store.upsert_project(DEMO, "demo")
    notifier = ChangeNotifier()
    coord = RefreshCoordinator(
        store, cfg, reindex_fn=lambda s, c: IndexStats(), on_change=notifier.bump
    )
    app = create_app(store, cfg, refresh_coordinator=coord, notifier=notifier)
    client = TestClient(app)

    jid = client.post("/api/schedule", json={
        "project_path": DEMO, "prompt": "do it",
        "scheduled_for": 1000, "mode": "background",
    }).json()["id"]

    before = notifier.revision
    r = client.patch(f"/api/schedule/{jid}", json={"prompt": "y"})
    assert r.status_code == 200
    assert notifier.revision > before, "PATCH /api/schedule/{id} must bump the notifier"
    store.close()
