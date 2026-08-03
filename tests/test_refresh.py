import threading
import time

import pytest

from bridge.config import load
from bridge.refresh import RefreshCoordinator
from bridge.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "refresh.db")
    yield value
    value.close()


def test_refresh_runs_do_not_overlap(store, tmp_path):
    cfg = load({"db_path": tmp_path / "refresh.db", "spool_dir": tmp_path / "spool"})
    active = 0
    maximum = 0
    lock = threading.Lock()

    def fake_reindex(_store, _cfg):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        from bridge.indexer import IndexStats

        return IndexStats(files_seen=1)

    coordinator = RefreshCoordinator(store, cfg, fake_reindex)
    threads = [threading.Thread(target=coordinator.run_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert maximum == 1
    assert coordinator.status_snapshot().generation == 2


def test_refresh_failure_is_unavailable_but_keeps_last_success(store, tmp_path):
    cfg = load({"db_path": tmp_path / "refresh.db", "spool_dir": tmp_path / "spool"})
    from bridge.indexer import IndexStats

    calls = [IndexStats(files_seen=1)]

    def fake_reindex(*_):
        if calls:
            return calls.pop()
        raise RuntimeError("disk unavailable")

    coordinator = RefreshCoordinator(store, cfg, fake_reindex)
    assert coordinator.run_once().completed
    result = coordinator.run_once()

    assert not result.completed
    status = coordinator.status_snapshot()
    assert status.generation == 1
    assert status.server == "unavailable"
    assert status.error == "disk unavailable"


def test_periodic_refresh_runs_immediately_and_stops(store, tmp_path):
    cfg = load({"db_path": tmp_path / "refresh.db", "spool_dir": tmp_path / "spool"})
    from bridge.indexer import IndexStats

    calls = []
    stop = threading.Event()
    stop.set()
    coordinator = RefreshCoordinator(
        store, cfg, lambda *_: (calls.append(1) or IndexStats()), interval_s=0
    )
    coordinator.run_periodic(stop)
    assert calls == [1]


def test_status_snapshot_is_immutable(store, tmp_path):
    cfg = load({"db_path": tmp_path / "refresh.db", "spool_dir": tmp_path / "spool"})
    status = RefreshCoordinator(store, cfg).status_snapshot()
    with pytest.raises(AttributeError):
        status.generation = 4
