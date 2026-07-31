import sqlite3
import threading

import pytest

from bridge.models import SessionRecord
from bridge.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "sub" / "t.db")
    yield s
    s.close()


def rec(sid="s1", **kw):
    base = dict(
        session_id=sid, transcript_path=f"/t/{sid}.jsonl",
        project_path="/Users/mitsheth/dev/demo", title="T",
        started_at="2026-07-30T10:00:00.000Z", ended_at="2026-07-30T11:00:00.000Z",
        model="claude-opus-5", effort="high", git_branch="main",
        user_msgs=1, assistant_msgs=2, tokens_in=5, tokens_out=6,
    )
    base.update(kw)
    return SessionRecord(**base)


def test_wal_and_foreign_keys_enabled(store):
    assert store.conn.execute("pragma journal_mode").fetchone()[0].lower() == "wal"
    assert store.conn.execute("pragma foreign_keys").fetchone()[0] == 1


def test_creates_parent_directory(tmp_path):
    s = Store(tmp_path / "deep" / "nested" / "b.db")
    assert (tmp_path / "deep" / "nested" / "b.db").exists()
    s.close()


def test_upsert_project_is_idempotent(store):
    a = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    b = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    assert a == b
    assert len(store.projects()) == 1


def test_hidden_projects_excluded_by_default(store):
    pid = store.upsert_project("/x", "x")
    store.set_project_status(pid, "hidden")
    assert store.projects() == []
    assert len(store.projects(include_hidden=True)) == 1


def test_upsert_session_is_idempotent_and_updates(store):
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(rec(), pid)
    store.upsert_session(rec(title="Updated", tokens_out=99), pid)
    rows = store.sessions(pid)
    assert len(rows) == 1
    assert rows[0]["title"] == "Updated"
    assert rows[0]["tokens_out"] == 99


def test_latest_session_is_most_recent_by_ended_at(store):
    pid = store.upsert_project("/d", "d")
    store.upsert_session(rec("old", ended_at="2026-07-01T00:00:00.000Z"), pid)
    store.upsert_session(rec("new", ended_at="2026-07-30T00:00:00.000Z"), pid)
    assert store.latest_session(pid)["id"] == "new"


def test_scan_state_roundtrip(store):
    assert store.get_scan_state("/t/a.jsonl") is None
    store.set_scan_state("/t/a.jsonl", 100, 1.5, 90, "s1")
    row = store.get_scan_state("/t/a.jsonl")
    assert (row["size"], row["parsed_offset"], row["session_id"]) == (100, 90, "s1")
    store.set_scan_state("/t/a.jsonl", 200, 2.5, 190, "s1")
    assert store.get_scan_state("/t/a.jsonl")["parsed_offset"] == 190


def test_token_totals_respects_since(store):
    pid = store.upsert_project("/d", "d")
    store.upsert_session(rec("a", ended_at="2026-07-30T10:00:00.000Z",
                             tokens_in=10, tokens_out=10), pid)
    store.upsert_session(rec("b", ended_at="2026-01-01T00:00:00.000Z",
                             tokens_in=99, tokens_out=99), pid)
    # 2026-07-30T00:00:00Z == 1785369600
    assert store.token_totals(pid, 1785369600) == 20


def test_concurrent_writers_do_not_error(tmp_path):
    """Proves the WAL + busy_timeout assumption the architecture rests on."""
    db = tmp_path / "c.db"
    main = Store(db)
    pid = main.upsert_project("/d", "d")
    errors: list[Exception] = []

    def worker(n: int):
        s = Store(db)
        try:
            for i in range(20):
                s.upsert_session(rec(f"s{n}-{i}"), pid)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(main.sessions(pid, limit=1000)) == 80
    main.close()


def test_additive_migration_preserves_data(tmp_path):
    db = tmp_path / "m.db"
    s = Store(db)
    pid = s.upsert_project("/d", "d")
    s.upsert_session(rec(), pid)
    s.close()
    # Re-opening applies migrations against a populated DB without loss.
    s2 = Store(db)
    assert len(s2.sessions(pid)) == 1
    s2.close()
