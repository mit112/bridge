import bridge.update as U


def test_lock_rejects_concurrent(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    fd = U._acquire_lock()
    assert fd is not None
    assert U._acquire_lock() is None    # second acquire is refused
    U._release_lock(fd)
    fd2 = U._acquire_lock()             # released -> acquirable again
    assert fd2 is not None
    U._release_lock(fd2)


def test_state_roundtrips(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    st = U.UpdateState(state="behind", installed_sha="a" * 40,
                       latest_sha="b" * 40, checked_at="2026-08-08T00:00:00+00:00",
                       error=None)
    U.write_update_state(st)
    assert U.read_update_state() == st


def test_read_state_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_update_dir", lambda: tmp_path)
    assert U.read_update_state() is None


def test_now_iso_is_utc():
    assert U._now_iso().endswith("+00:00")
