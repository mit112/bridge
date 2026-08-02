import sqlite3
import threading

import pytest

from bridge.models import Handoff, Launch, SessionRecord
from bridge.store import Store, to_epoch
from tests.conftest import launch_by_session


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


# Pre-assigned session ids, in the lowercase 8-4-4-4-12 form `claude` validates.
SID_A = "aaaaaaaa-1111-4111-8111-111111111111"
SID_B = "bbbbbbbb-2222-4222-8222-222222222222"


def launch(project_id, lid="l1", **kw):
    base = dict(
        id=lid, project_id=project_id, mode="terminal", prompt="do the thing",
        handoff_id="h1", session_id=SID_A, model="claude-opus-5", effort="high",
        launched_at=2000,
    )
    base.update(kw)
    return Launch(**base)


def _job(store, sid="j1", scheduled_for=1000, **kw):
    from bridge.models import ScheduledRun
    job = ScheduledRun(id=sid, project_path="/p", prompt="go", mode="background",
                       scheduled_for=scheduled_for, created_at=500, **kw)
    return store.create_scheduled_run(job)


def test_claim_one_due_claims_exactly_one_pending_job_at_or_before_now(store):
    _job(store, "a", scheduled_for=1000)
    _job(store, "b", scheduled_for=2000)          # future
    row = store.claim_one_due(now=1500)
    assert row["id"] == "a" and row["status"] == "launching" and row["claimed_at"] == 1500
    assert store.claim_one_due(now=1500) is None    # b is future; a already launching


def test_claim_one_due_is_a_single_winner_under_a_repeat_claim(store):
    _job(store, "a", scheduled_for=1000)
    assert store.claim_one_due(now=1500)["id"] == "a"
    assert store.claim_one_due(now=1500) is None     # already launching, not re-claimed


def test_claim_one_due_fires_a_job_due_exactly_at_now(store):
    _job(store, "a", scheduled_for=1500)      # due exactly at `now`
    row = store.claim_one_due(now=1500)
    assert row is not None and row["id"] == "a"   # `<=`, not `<`: due-now must fire


def test_finish_requires_a_prior_launching_and_records_terminal_fields(store):
    _job(store, "a", scheduled_for=1000)
    store.claim_one_due(now=1500)
    store.finish_scheduled_run("a", status="fired", launch_id="L1", fired_at=1600)
    r = store.get_scheduled_run("a")
    assert r["status"] == "fired" and r["launch_id"] == "L1" and r["fired_at"] == 1600
    assert r["completed_at"] is not None


def test_edit_and_cancel_only_touch_pending(store):
    _job(store, "a", scheduled_for=1000)
    assert store.edit_pending("a", prompt="new", scheduled_for=1200) is True
    assert store.get_scheduled_run("a")["prompt"] == "new"
    store.claim_one_due(now=1500)
    assert store.edit_pending("a", prompt="later") is False   # launching, immutable
    assert store.cancel_pending("a") is False


def test_cancel_pending_marks_cancelled(store):
    _job(store, "a", scheduled_for=1000)
    assert store.cancel_pending("a") is True
    assert store.get_scheduled_run("a")["status"] == "cancelled"
    assert store.claim_one_due(now=1500) is None              # cancelled never claimed


def test_reconcile_launching_flips_strays_to_indeterminate(store):
    _job(store, "a", scheduled_for=1000)
    store.claim_one_due(now=1500)                             # leaves it 'launching'
    assert store.reconcile_launching(now=9000) == 1
    assert store.get_scheduled_run("a")["status"] == "indeterminate"


def test_claim_specific_claims_a_pending_job_by_id(store):
    _job(store, "a", scheduled_for=9999)                      # far future
    assert store.claim_specific("a")["status"] == "launching"
    assert store.claim_specific("a") is None                 # not pending anymore


def test_finish_scheduled_run_is_a_noop_when_not_launching(store):
    _job(store, "a", scheduled_for=1000)
    assert store.cancel_pending("a") is True                 # status is now 'cancelled'
    store.finish_scheduled_run("a", status="fired", launch_id="L1", fired_at=1600)
    r = store.get_scheduled_run("a")
    assert r["status"] == "cancelled"
    assert r["launch_id"] is None
    assert r["fired_at"] is None


def _terminal(store, sid, status="failed", **kw):
    """Drive a job all the way to a terminal status the honest way."""
    _job(store, sid, **kw)
    store.claim_specific(sid)
    store.finish_scheduled_run(sid, status=status, error="boom")
    return sid


def test_retry_terminal_copies_a_failed_job_into_a_new_claimed_row(store):
    """The whole point of the endpoint: `source_handoff_id` survives the retry.

    The panel's old retry POSTed `/api/launch` with no handoff at all, so a
    schedule created FROM a handoff could be retried successfully and still
    leave the original handoff sitting queued forever.
    """
    _terminal(store, "a", source_handoff_id="h1", summary="s", model="m",
              effort="high", permission_mode="acceptEdits")
    row = store.retry_terminal("a", new_id="a-retry", now=7000)
    assert row is not None
    assert row["id"] == "a-retry"
    assert row["retry_of"] == "a"
    assert row["source_handoff_id"] == "h1"
    assert row["status"] == "launching" and row["claimed_at"] == 7000
    # Claimed at creation, so `_fire_claimed_job` can take it with no second
    # transition -- and `scheduled_for` is now, because it is firing now.
    assert row["scheduled_for"] == 7000 and row["created_at"] == 7000
    for column in ("project_path", "prompt", "summary", "model", "effort",
                   "mode", "permission_mode"):
        assert row[column] == store.get_scheduled_run("a")[column]
    # The failure itself is history, not something a retry overwrites.
    original = store.get_scheduled_run("a")
    assert original["status"] == "failed" and original["error"] == "boom"


def test_retry_terminal_also_recovers_an_indeterminate_job(store):
    """A crash-stranded job is never auto-retried, so a manual one is the
    only recovery path it has."""
    _job(store, "a", scheduled_for=1000)
    store.claim_one_due(now=1500)
    store.reconcile_launching(now=9000)
    assert store.get_scheduled_run("a")["status"] == "indeterminate"
    assert store.retry_terminal("a", new_id="a2", now=9500) is not None


@pytest.mark.parametrize("prepare", [
    lambda s: _job(s, "a"),                                   # pending
    lambda s: (_job(s, "a"), s.claim_specific("a")),          # launching
    lambda s: _terminal(s, "a", status="fired"),              # already succeeded
    lambda s: (_job(s, "a"), s.cancel_pending("a")),          # dismissed on purpose
])
def test_retry_terminal_refuses_anything_but_a_failed_or_indeterminate_job(store, prepare):
    prepare(store)
    assert store.retry_terminal("a", new_id="a2", now=7000) is None
    assert store.get_scheduled_run("a2") is None


def test_retry_terminal_refuses_a_second_retry_of_the_same_job(store):
    """Two tabs, or a double click that outran the button's own disable, must
    not produce two launches from one failure."""
    _terminal(store, "a")
    assert store.retry_terminal("a", new_id="a2", now=7000) is not None
    assert store.retry_terminal("a", new_id="a3", now=7001) is None
    assert store.get_scheduled_run("a3") is None


def test_a_retry_that_failed_can_itself_be_retried(store):
    """One retry per row, chained -- the row a user sees failing is always the
    newest one, and that is the one its Retry button names."""
    _terminal(store, "a")
    store.retry_terminal("a", new_id="a2", now=7000)
    store.finish_scheduled_run("a2", status="failed", error="again")
    row = store.retry_terminal("a2", new_id="a3", now=7100)
    assert row is not None and row["retry_of"] == "a2"


def test_retry_terminal_is_none_for_an_id_nothing_ever_created(store):
    assert store.retry_terminal("nope", new_id="x", now=7000) is None


def test_scheduled_runs_pages_without_losing_the_active_first_order(store):
    for i in range(5):
        _job(store, f"j{i}", scheduled_for=1000 + i)
    store.claim_specific("j4")
    store.finish_scheduled_run("j4", status="fired")

    everything = [r["id"] for r in store.scheduled_runs()]
    assert everything[-1] == "j4"                       # terminal sinks to the end
    assert [r["id"] for r in store.scheduled_runs(limit=2)] == everything[:2]
    assert [r["id"] for r in store.scheduled_runs(limit=2, offset=2)] == everything[2:4]
    # An offset with no limit is the rest of the list, not an empty page.
    assert [r["id"] for r in store.scheduled_runs(offset=3)] == everything[3:]
    assert store.count_scheduled_runs() == 5
    assert store.count_scheduled_runs(status="fired") == 1


def test_prune_scheduled_runs_deletes_only_old_terminal_rows(store):
    _terminal(store, "old-failed")
    store.conn.execute("UPDATE scheduled_runs SET completed_at=100 WHERE id='old-failed'")
    _job(store, "old-pending", scheduled_for=100)         # ancient, but still due
    _job(store, "claimed", scheduled_for=100)
    store.claim_specific("claimed")                        # launching: in flight
    _terminal(store, "recent-failed")                      # completed_at = now

    assert store.prune_scheduled_runs(before_epoch=1000) == 1
    assert store.get_scheduled_run("old-failed") is None
    for survivor in ("old-pending", "claimed", "recent-failed"):
        assert store.get_scheduled_run(survivor) is not None


def test_prune_scheduled_runs_reaps_cancelled_rows_too(store):
    _job(store, "a")
    store.cancel_pending("a")
    store.conn.execute("UPDATE scheduled_runs SET completed_at=100 WHERE id='a'")
    assert store.prune_scheduled_runs(before_epoch=1000) == 1


def test_pragmas_are_set(store):
    assert store.conn.execute("pragma journal_mode").fetchone()[0].lower() == "wal"
    assert store.conn.execute("pragma foreign_keys").fetchone()[0] == 1
    assert store.conn.execute("pragma busy_timeout").fetchone()[0] == 5000


def test_archive_missing_sets_status_and_stamps_when_we_acted(store):
    pid = store.upsert_project("/gone/for/good", "gone")
    store.archive_missing(pid, at=1_780_000_000)
    row = store.get_project(pid)
    assert row["status"] == "archived"
    assert row["missing_archived_at"] == 1_780_000_000


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

    still_queued = store.queued_handoff(pid)
    assert still_queued is not None, (
        "rollback left the project with nothing queued: the supersede committed "
        "without the insert"
    )
    assert still_queued["id"] == "h1"
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


# --- Phase 3: the launcher ---------------------------------------------------


def test_a_launch_round_trips_and_is_found_by_its_session_id(store):
    """The terminal-mode path: the id is pre-assigned, so the row is the join.

    Two launches exist so the join has to *select* rather than return the only
    row there is.
    """
    pid = store.upsert_project("/d", "d")
    store.create_handoff(handoff("h1"), pid)
    store.create_launch(launch(pid, "l1"))
    store.create_launch(launch(pid, "l2", session_id=SID_B, launched_at=3000))

    row = launch_by_session(store, SID_A)
    assert row["id"] == "l1"
    assert (row["project_id"], row["handoff_id"], row["mode"]) == (pid, "h1", "terminal")
    assert (row["model"], row["effort"]) == ("claude-opus-5", "high")
    assert row["prompt"] == "do the thing"
    assert row["launched_at"] == 2000
    assert row["outcome"] == "pending", "the row is written before the spawn"

    store.set_launch_outcome("l1", "started")
    assert launch_by_session(store, SID_A)["outcome"] == "started"
    assert launch_by_session(store, SID_B)["outcome"] == "pending", "one row moved"
    assert [r["id"] for r in store.launches(pid)] == ["l2", "l1"]


def test_a_launch_needs_no_handoff_behind_it(store):
    """An ad-hoc prompt typed into the panel has no queued handoff to consume."""
    pid = store.upsert_project("/d", "d")
    store.create_launch(launch(pid, "l1", handoff_id=None))
    assert launch_by_session(store, SID_A)["handoff_id"] is None


def test_a_launch_cannot_reference_a_handoff_that_does_not_exist(store):
    """The FK is enforced, so `launches.handoff_id` can never dangle."""
    pid = store.upsert_project("/d", "d")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_launch(launch(pid, "l1", handoff_id="no-such-handoff"))


def test_set_launch_session_fills_in_both_ids_after_a_background_spawn(store):
    """`claude --bg` mints its own id, so the row starts with neither.

    A NOT NULL `session_id` would force a placeholder here, and a placeholder in
    a correlation key is how a launch joins to the wrong session.
    """
    pid = store.upsert_project("/d", "d")
    store.create_launch(
        launch(pid, "l1", mode="background", session_id=None, handoff_id=None)
    )
    row = store.launches(pid)[0]
    assert (row["session_id"], row["short_id"]) == (None, None)
    assert launch_by_session(store, SID_A) is None

    store.set_launch_session("l1", SID_A, SID_A[:8])
    row = launch_by_session(store, SID_A)
    assert row["id"] == "l1"
    assert row["short_id"] == "aaaaaaaa"


# --- Phase 4 Task 6: the seven-day token series ------------------------------

SERIES_NOW = 1785600000


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone

    return (datetime.fromtimestamp(epoch, timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.000Z"))


def _sess(store, pid, sid, ended_epoch, tokens_in=0, tokens_out=0, **kw):
    store.upsert_session(
        SessionRecord(session_id=sid, transcript_path=f"/t/{sid}",
                      ended_at=_iso(ended_epoch),
                      tokens_in=tokens_in, tokens_out=tokens_out, **kw),
        pid,
    )


def test_token_series_returns_one_bucket_per_day_oldest_first(store):
    pid = store.upsert_project("/p/series", "series")
    _sess(store, pid, "t-today-a", SERIES_NOW - 100, tokens_in=100, tokens_out=100)
    _sess(store, pid, "t-today-b", SERIES_NOW - 200, tokens_in=50, tokens_out=50)
    _sess(store, pid, "t-3ago", SERIES_NOW - 3 * 86400 - 100, tokens_in=25, tokens_out=25)

    series = store.token_series(pid, days=7, now=SERIES_NOW)

    assert len(series) == 7
    assert series[-1] == 300   # today is last
    assert series[3] == 50     # three days ago
    assert series[0] == 0      # no activity that day, present as a zero


def test_token_series_pads_a_project_with_no_activity_at_all(store):
    pid = store.upsert_project("/p/quiet", "quiet")
    assert store.token_series(pid, days=7, now=SERIES_NOW) == [0] * 7


def test_token_series_excludes_activity_older_than_the_window(store):
    pid = store.upsert_project("/p/old", "old")
    _sess(store, pid, "t-old", SERIES_NOW - 30 * 86400, tokens_in=999, tokens_out=999)
    assert store.token_series(pid, days=7, now=SERIES_NOW) == [0] * 7


def test_token_series_does_not_leak_another_project_s_burn(store):
    mine = store.upsert_project("/p/mine", "mine")
    theirs = store.upsert_project("/p/theirs", "theirs")
    _sess(store, theirs, "t-theirs", SERIES_NOW - 100, tokens_in=500, tokens_out=500)
    assert store.token_series(mine, days=7, now=SERIES_NOW) == [0] * 7


def test_the_series_uses_the_same_token_definition_as_the_burn_text(store):
    """The sparkline sits inches from `token_totals`'s "23k today".

    `token_totals` sums tokens_in + tokens_out only. A series that also summed
    the cache columns would draw a line whose shape contradicts the number
    printed beside it, and nothing would ever flag it.
    """
    pid = store.upsert_project("/p/defn", "defn")
    _sess(store, pid, "t-defn", SERIES_NOW - 100, tokens_in=10, tokens_out=5,
          tokens_cache_create=1000, tokens_cache_read=2000)

    assert store.token_series(pid, days=1, now=SERIES_NOW)[-1] == 15
    assert (store.token_series(pid, days=1, now=SERIES_NOW)[-1]
            == store.token_totals(pid, SERIES_NOW - 86400))
