import threading

import bridge.update as U


def test_ensure_cache_repo_none_without_git(monkeypatch):
    monkeypatch.setattr(U.shutil, "which", lambda _: None)
    assert U._ensure_cache_repo() is None


def test_check_once_behind(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    monkeypatch.setattr(U, "classify", lambda i, r: "behind")
    ck = U.UpdateChecker(enabled=True, resolve_fn=lambda **k: "b" * 40)
    st = ck.check_once()
    assert st.state == "behind"
    assert st.latest_sha == "b" * 40
    assert st.installed_sha == "a" * 40
    assert st.error is None
    assert st.checked_at is not None


def test_check_once_fail_closed_keeps_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    ck = U.UpdateChecker(enabled=True, resolve_fn=lambda **k: "b" * 40)
    monkeypatch.setattr(U, "classify", lambda i, r: "behind")
    ck.check_once()                                   # prime a good result
    ck2 = U.UpdateChecker(enabled=True, resolve_fn=lambda **k: None)  # network fails
    st = ck2.check_once()
    assert st.state == "stale"                        # never "behind" on failure
    assert st.error is not None
    assert st.latest_sha == "b" * 40                  # last known kept


def test_disabled_never_calls_network(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    called = []
    ck = U.UpdateChecker(enabled=False,
                         resolve_fn=lambda **k: called.append(1) or "b" * 40)
    st = ck.check_once()
    assert called == []
    assert st.state == "unknown"


def test_run_periodic_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    monkeypatch.setattr(U, "classify", lambda i, r: "current")
    ck = U.UpdateChecker(enabled=True, interval_s=0.01,
                         resolve_fn=lambda **k: "a" * 40)
    stop = threading.Event()
    t = threading.Thread(target=ck.run_periodic, args=(stop,))
    t.start()
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
