import sqlite3
import threading

import pytest

from bridge.models import Handoff, SessionRecord
from bridge.store import Store, to_epoch


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


def handoff(hid="h1", **kw):
    base = dict(
        id=hid, project_path="/Users/mitsheth/dev/demo",
        next_prompt="pick up where this left off", summary="did some work",
        source_session_id="sess-1", suggested_model="claude-opus-5",
        suggested_effort="high", created_at=1000,
    )
    base.update(kw)
    return Handoff(**base)


def test_pragmas_are_set(store):
    assert store.conn.execute("pragma journal_mode").fetchone()[0].lower() == "wal"
    assert store.conn.execute("pragma foreign_keys").fetchone()[0] == 1
    assert store.conn.execute("pragma busy_timeout").fetchone()[0] == 5000


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


def test_upsert_session_updates_every_mutable_column(store):
    """Every column in the ON CONFLICT clause must actually update.

    Dropping any single column from the clause must fail this test.
    """
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(rec(), pid)

    updated = rec(
        title="Updated", started_at="2026-07-31T01:00:00.000Z",
        ended_at="2026-07-31T02:00:00.000Z", model="claude-sonnet-5",
        effort="low", git_branch="feature", user_msgs=7, assistant_msgs=9,
        last_prompt="next thing", tokens_in=11, tokens_out=22,
        tokens_cache_create=33, tokens_cache_read=44, sidechain_tokens=55,
        interrupted=True, transcript_path="/t/moved.jsonl",
    )
    store.upsert_session(updated, pid)

    rows = store.sessions(pid)
    assert len(rows) == 1
    row = rows[0]
    for column, expected in [
        ("title", "Updated"),
        ("started_at", "2026-07-31T01:00:00.000Z"),
        ("ended_at", "2026-07-31T02:00:00.000Z"),
        ("model", "claude-sonnet-5"),
        ("effort", "low"),
        ("git_branch", "feature"),
        ("user_msgs", 7),
        ("assistant_msgs", 9),
        ("last_prompt", "next thing"),
        ("tokens_in", 11),
        ("tokens_out", 22),
        ("tokens_cache_create", 33),
        ("tokens_cache_read", 44),
        ("sidechain_tokens", 55),
        ("interrupted", 1),
        ("transcript_path", "/t/moved.jsonl"),
    ]:
        assert row[column] == expected, column
    assert row["ended_epoch"] == to_epoch("2026-07-31T02:00:00.000Z")


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
    store.upsert_session(rec("c", ended_at="2026-07-30T00:00:00.000Z",
                             tokens_in=3, tokens_out=4), pid)
    # 2026-07-30T00:00:00Z == 1785369600
    # 20 from "a" + 7 from "c" sitting exactly on the cutoff (>= is inclusive)
    assert store.token_totals(pid, 1785369600) == 27


def test_concurrent_writers_do_not_error_or_lose_rows(tmp_path):
    """Concurrent writers across connections neither error nor lose rows.

    This exercises real thread interleaving and row-count integrity. It does
    NOT isolate `journal_mode=WAL` or `PRAGMA busy_timeout`: Python's
    `sqlite3.connect(timeout=...)` supplies an equivalent busy-retry, so this
    test passes with both PRAGMAs removed. Their values are asserted directly
    in test_pragmas_are_set instead.
    """
    db = tmp_path / "c.db"
    main = Store(db)
    pid = main.upsert_project("/d", "d")
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker(n: int):
        s = Store(db)
        try:
            barrier.wait()  # All four threads start simultaneously
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


def test_reopen_preserves_data(tmp_path):
    """Reopening an existing DB replays SCHEMA idempotently without loss."""
    db = tmp_path / "m.db"
    s = Store(db)
    pid = s.upsert_project("/d", "d")
    s.upsert_session(rec(), pid)
    s.close()
    s2 = Store(db)
    assert len(s2.sessions(pid)) == 1
    s2.close()


def test_additive_column_migration_is_idempotent(tmp_path, monkeypatch):
    """A newly appended column must apply once and survive later opens.

    SQLite has no ADD COLUMN IF NOT EXISTS, so a naive schema replay would
    raise `duplicate column name` on the second open. This is the test that
    makes the spec's additive-migration doctrine real.
    """
    import bridge.store as store_mod

    db = tmp_path / "mig.db"
    s = Store(db)
    pid = s.upsert_project("/d", "d")
    s.upsert_session(rec(), pid)
    s.close()

    monkeypatch.setattr(
        store_mod, "COLUMN_MIGRATIONS", {"sessions": {"note": "TEXT"}}
    )

    s2 = Store(db)  # applies the ALTER
    cols = {r["name"] for r in s2.conn.execute("PRAGMA table_info(sessions)")}
    assert "note" in cols
    assert len(s2.sessions(pid)) == 1  # data preserved
    s2.close()

    s3 = Store(db)  # must NOT raise "duplicate column name"
    assert len(s3.sessions(pid)) == 1
    s3.close()


def test_transaction_rolls_back_on_error(store):
    """A failed transaction must leave neither write applied."""
    pid = store.upsert_project("/d", "d")
    try:
        with store.transaction():
            store.set_scan_state("/t/x.jsonl", 100, 1.0, 50, "sx")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert store.get_scan_state("/t/x.jsonl") is None


def test_transaction_commits_both_writes(store):
    pid = store.upsert_project("/d", "d")
    with store.transaction():
        store.set_scan_state("/t/y.jsonl", 100, 1.0, 50, "sy")
        store.upsert_session(rec("sy"), pid)
    assert store.get_scan_state("/t/y.jsonl")["parsed_offset"] == 50
    assert len(store.sessions(pid)) == 1


def test_alias_map_round_trips_what_was_set(store):
    store.set_alias("/old/a", "/new/a")
    store.set_alias("/old/b", "/new/b")
    assert store.alias_map() == {"/old/a": "/new/a", "/old/b": "/new/b"}


def test_alias_map_is_empty_before_anything_is_seeded(store):
    assert store.alias_map() == {}


def test_reseeding_an_alias_replaces_its_target(store):
    """Seeding runs on every index, so the same alias is written repeatedly.
    It must update in place rather than raising on the primary key."""
    store.set_alias("/old/a", "/new/a")
    store.set_alias("/old/a", "/newer/a")
    assert store.alias_map() == {"/old/a": "/newer/a"}


def test_second_handoff_supersedes_the_first_and_both_survive(store):
    """A card shows exactly one next step, but nothing is thrown away."""
    pid = store.upsert_project("/d", "d")
    store.create_handoff(handoff("h1", created_at=100), pid)
    store.create_handoff(handoff("h2", created_at=200), pid)

    assert store.queued_handoff(pid)["id"] == "h2"
    assert store.get_handoff("h1")["status"] == "superseded"
    assert {r["id"] for r in store.handoffs(pid)} == {"h1", "h2"}
    queued = [r["id"] for r in store.handoffs(pid) if r["status"] == "queued"]
    assert queued == ["h2"], "exactly one handoff may be queued per project"


def test_supersede_and_insert_are_atomic(store):
    """If the insert fails, the previous handoff must still be queued.

    Superseding outside the transaction would leave the project with its old
    prompt retired and no new one queued — worse than either outcome alone. A
    NOT NULL violation on next_prompt stands in for any insert failure.
    """
    pid = store.upsert_project("/d", "d")
    store.create_handoff(handoff("h1"), pid)

    with pytest.raises(sqlite3.IntegrityError):
        store.create_handoff(handoff("h2", next_prompt=None), pid)

    assert store.queued_handoff(pid)["id"] == "h1"
    assert store.get_handoff("h1")["status"] == "queued"
    assert store.get_handoff("h2") is None


def test_reinserting_the_queued_handoff_does_not_supersede_itself(store):
    """The re-drain case. Without the `id<>?` guard, ingesting the same spool
    file twice supersedes the row it is re-inserting and the project ends up
    with nothing queued — silently losing the prompt it was protecting."""
    pid = store.upsert_project("/d", "d")
    store.create_handoff(handoff("h1"), pid)
    store.create_handoff(handoff("h1"), pid)

    assert store.queued_handoff(pid) is not None
    assert store.queued_handoff(pid)["id"] == "h1"
    assert len(store.handoffs(pid)) == 1


def test_reinserting_a_consumed_handoff_does_not_resurrect_it(store):
    pid = store.upsert_project("/d", "d")
    store.create_handoff(handoff("h1"), pid)
    store.set_handoff_status("h1", "consumed")
    store.create_handoff(handoff("h1"), pid)
    assert store.get_handoff("h1")["status"] == "consumed"
    assert store.queued_handoff(pid) is None


def test_handoffs_are_scoped_to_their_project(store):
    a = store.upsert_project("/a", "a")
    b = store.upsert_project("/b", "b")
    store.create_handoff(handoff("ha", project_path="/a"), a)
    store.create_handoff(handoff("hb", project_path="/b"), b)
    # Queueing for one project must not supersede another's.
    assert store.queued_handoff(a)["id"] == "ha"
    assert store.queued_handoff(b)["id"] == "hb"


def test_consumed_at_is_stamped_only_by_the_consumed_transition(store):
    pid = store.upsert_project("/d", "d")
    store.create_handoff(handoff("h1"), pid)
    store.set_handoff_status("h1", "dismissed")
    assert store.get_handoff("h1")["consumed_at"] is None
    store.set_handoff_status("h1", "consumed")
    stamped = store.get_handoff("h1")["consumed_at"]
    assert stamped is not None
    store.set_handoff_status("h1", "queued")  # must not clear the stamp
    assert store.get_handoff("h1")["consumed_at"] == stamped


def test_a_handoff_requires_a_real_project(store):
    """The FK is enforced, so a handoff can never dangle off a missing project."""
    with pytest.raises(sqlite3.IntegrityError):
        store.create_handoff(handoff("h1"), 424242)


def test_upsert_project_does_not_reset_an_existing_status(store):
    """Re-indexing upserts every project it sees, so an archived project must
    survive the upsert. This is what makes a status set from outside the
    config's archive list durable."""
    pid = store.upsert_project("/d", "d")
    store.set_project_status(pid, "archived")
    assert store.upsert_project("/d", "d") == pid
    assert store.get_project(pid)["status"] == "archived"
