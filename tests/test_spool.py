import json
import logging
import os
from pathlib import Path

import pytest

from bridge import spool
from bridge.models import Handoff
from bridge.store import Store

DEMO = "/Users/you/dev/demo"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db" / "s.db")
    yield s
    s.close()


@pytest.fixture
def spool_dir(tmp_path):
    return tmp_path / "spool"


def h(hid="h1", **kw):
    base = dict(
        id=hid, project_path=DEMO, next_prompt="continue the work",
        summary="a summary", source_session_id="sess-1", created_at=1000,
    )
    base.update(kw)
    return Handoff(**base)


def demo_pid(store):
    row = store.project_by_path(DEMO)
    assert row is not None, "drain should have created the project"
    return row["id"]


# --- write -------------------------------------------------------------------


def test_write_produces_one_named_file_and_no_leftovers(spool_dir):
    p = spool.write(h(), spool_dir)
    assert p.name == "h1.json"
    # iterdir rather than glob: a leftover temp file may be dot-prefixed, and
    # glob's dotfile handling is exactly what we must not depend on here.
    assert [f.name for f in spool_dir.iterdir()] == ["h1.json"]
    assert spool.pending_count(spool_dir) == 1
    assert json.loads(p.read_text())["next_prompt"] == "continue the work"


def test_an_interrupted_write_leaves_no_readable_final_file(spool_dir, monkeypatch):
    """`os.replace` is what makes this true.

    A crash mid-write may leave a temp file, but it must never leave a partial
    `<id>.json` for the drain to read. Writing in place instead would publish a
    truncated prompt under the final name.
    """
    def boom(fd):
        raise OSError("simulated disk full")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        spool.write(h(next_prompt="x" * 100_000), spool_dir)

    assert spool.pending(spool_dir) == []
    assert not (spool_dir / "h1.json").exists()
    assert [f.name for f in spool_dir.iterdir()] == []


# --- Codex review finding #19: the rename itself must be durable, not just
#     the file's contents -------------------------------------------------
#
# `_atomic_write` fsynced the temp file before `os.replace`, which makes the
# CONTENTS durable, but never fsynced the directory the rename landed in --
# on some filesystems the rename (the directory entry pointing at the new
# name) can still be lost to a power loss even though the file's bytes are
# safe on disk.
def test_write_fsyncs_the_containing_directory_after_the_rename(spool_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(spool, "fsync_dir", lambda d: calls.append(Path(d)))

    spool.write(h("h1"), spool_dir)

    assert calls == [spool_dir]


def test_pending_ignores_the_drained_and_bad_subdirectories(store, spool_dir):
    spool.write(h("h1"), spool_dir)
    (spool_dir / "broken.json").write_text("{nope")
    spool.drain(store, spool_dir)
    assert (spool_dir / "drained" / "h1.json").exists()
    assert (spool_dir / "bad" / "broken.json").exists()
    assert spool.pending(spool_dir) == []
    assert spool.pending_count(spool_dir) == 0


# --- drain -------------------------------------------------------------------


def test_drain_ingests_a_spooled_handoff_and_creates_the_project(store, spool_dir):
    spool.write(h(), spool_dir)
    stats = spool.drain(store, spool_dir)
    assert (stats.drained, stats.bad, stats.failed) == (1, 0, 0)
    row = store.queued_handoff(demo_pid(store))
    assert row["id"] == "h1"
    assert row["next_prompt"] == "continue the work"
    assert row["source_session_id"] == "sess-1"
    assert row["created_at"] == 1000


def test_drain_is_idempotent(store, spool_dir):
    """A re-drained file collides on primary key and is ignored, not duplicated.

    This is why the CLI mints the id rather than the server.
    """
    spool.write(h("h1"), spool_dir)
    assert spool.drain(store, spool_dir).drained == 1
    pid = demo_pid(store)
    assert len(store.handoffs(pid)) == 1

    # Restore the journal file into the live spool: what a crash between insert
    # and move, or a manual replay, looks like.
    (spool_dir / "h1.json").write_text((spool_dir / "drained" / "h1.json").read_text())
    spool.drain(store, spool_dir)

    assert len(store.handoffs(pid)) == 1
    assert store.queued_handoff(pid)["id"] == "h1"


def test_a_corrupt_file_is_quarantined_and_the_valid_ones_still_drain(store, spool_dir):
    spool.write(h("good1", created_at=1), spool_dir)
    spool.write(h("good2", created_at=2), spool_dir)
    (spool_dir / "broken.json").write_text("{not json at all")
    (spool_dir / "missing-fields.json").write_text('{"id": "x"}')

    stats = spool.drain(store, spool_dir)

    assert (stats.drained, stats.bad) == (2, 2)
    assert (spool_dir / "bad" / "broken.json").exists()
    assert (spool_dir / "bad" / "missing-fields.json").exists()
    assert not (spool_dir / "broken.json").exists()
    pid = demo_pid(store)
    assert {r["id"] for r in store.handoffs(pid)} == {"good1", "good2"}
    assert store.queued_handoff(pid)["id"] == "good2"


def test_drain_replays_in_created_at_order_not_filename_order(store, spool_dir):
    """Filenames are uuids, so alphabetical order is arbitrary. Replaying out of
    order would supersede the newest prompt and leave an older one queued."""
    spool.write(h("aaa-is-newer", created_at=2000), spool_dir)
    spool.write(h("zzz-is-older", created_at=1000), spool_dir)

    spool.drain(store, spool_dir)

    pid = demo_pid(store)
    assert store.queued_handoff(pid)["id"] == "aaa-is-newer"
    assert store.get_handoff("zzz-is-older")["status"] == "superseded"


def test_drain_resolves_an_aliased_path_to_the_canonical_project(store, spool_dir):
    """`bridge handoff` run from an old ~/Documents cwd must not re-split the
    history that path aliasing just merged."""
    store.set_alias("/Users/you/Documents/projectX", "/Users/you/dev/projectX")
    spool.write(h("h1", project_path="/Users/you/Documents/projectX"), spool_dir)

    spool.drain(store, spool_dir)

    assert store.project_by_path("/Users/you/Documents/projectX") is None
    canonical = store.project_by_path("/Users/you/dev/projectX")
    assert canonical is not None
    assert store.queued_handoff(canonical["id"])["id"] == "h1"


def test_a_failed_insert_leaves_the_file_spooled_for_the_next_boot(store, spool_dir):
    """A transient database failure must not consume the journal file."""
    spool.write(h("h1"), spool_dir)

    def explode(store_, path):
        raise RuntimeError("database is having a bad day")

    stats = spool.drain(store, spool_dir, resolve=explode)

    assert (stats.drained, stats.failed) == (0, 1)
    assert spool.pending_count(spool_dir) == 1
    assert not (spool_dir / "drained" / "h1.json").exists()
    # And the next attempt succeeds.
    assert spool.drain(store, spool_dir).drained == 1


def test_a_hostile_prompt_survives_write_and_drain_byte_for_byte(store, spool_dir):
    prompt = (
        'quotes " and \' and backticks `whoami`\n'
        "shell substitution $(echo pwned) and ${HOME}\n"
        "a windows path C:\\Users\\x and a tab\there\n"
        "unicode: émoji 🌉 and markup <script>alert(1)</script>\n"
        + "padding " * 5000
    )
    spool.write(h("h1", next_prompt=prompt), spool_dir)
    spool.drain(store, spool_dir)
    assert store.queued_handoff(demo_pid(store))["next_prompt"] == prompt


# --- the journal invariant ---------------------------------------------------


def test_drained_files_are_retained_and_can_rebuild_the_table(tmp_path, spool_dir):
    """The property that keeps `rm ~/.bridge/bridge.db` a safe operation.

    Handoffs are the first authored data Bridge stores, so the database alone is
    no longer a pure derived cache. The retained journal is what restores the
    invariant at the system level. If drain unlinks the file on success, the
    rebuild finds nothing and this fails.
    """
    db = tmp_path / "r.db"
    s1 = Store(db)
    spool.write(h("h1", created_at=1), spool_dir)
    spool.write(h("h2", created_at=2), spool_dir)
    assert spool.drain(s1, spool_dir).drained == 2
    assert len(s1.handoffs(demo_pid(s1))) == 2
    s1.close()

    assert {p.name for p in (spool_dir / "drained").glob("*.json")} == {
        "h1.json", "h2.json",
    }
    assert spool.pending(spool_dir) == []

    # Lose the database exactly as `rm ~/.bridge/bridge.db` would.
    db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    s2 = Store(db)
    assert s2.handoff_count() == 0
    stats = spool.rebuild_if_empty(s2, spool_dir)
    assert stats.drained == 2, "the retained journal must be sufficient on its own"
    pid = demo_pid(s2)
    assert {r["id"] for r in s2.handoffs(pid)} == {"h1", "h2"}
    assert s2.queued_handoff(pid)["id"] == "h2"
    s2.close()


def test_rebuild_is_skipped_when_the_table_is_not_empty(store, spool_dir):
    """Replaying the journal unconditionally would resurrect a consumed handoff
    as queued on every index, so a prompt already used would reappear forever."""
    spool.write(h("h1"), spool_dir)
    spool.drain(store, spool_dir)
    pid = demo_pid(store)
    store.set_handoff_status("h1", "consumed")

    stats = spool.rebuild_if_empty(store, spool_dir)

    assert stats.skipped == 1
    assert stats.drained == 0
    assert store.get_handoff("h1")["status"] == "consumed"
    assert store.queued_handoff(pid) is None


def test_rebuild_also_picks_up_anything_still_pending(tmp_path, spool_dir):
    """A wipe can coincide with an undrained spool file; neither source is lost."""
    db = tmp_path / "b.db"
    s = Store(db)
    spool.write(h("drained-one", created_at=1), spool_dir)
    spool.drain(s, spool_dir)
    s.close()
    db.unlink()
    spool.write(h("still-pending", created_at=2), spool_dir)

    s2 = Store(db)
    stats = spool.rebuild_if_empty(s2, spool_dir)
    assert stats.drained == 2
    pid = demo_pid(s2)
    assert {r["id"] for r in s2.handoffs(pid)} == {"drained-one", "still-pending"}
    assert s2.queued_handoff(pid)["id"] == "still-pending"
    s2.close()


# --- Phase 3: the launcher ---------------------------------------------------


# --- Codex review finding #11: an insert failure must not half-restore the
#     table and then permanently lock the rest out ---------------------------
#
# `stats.failed` already existed here, unlike schedspool's bare `except:
# pass` -- but the record was still never quarantined, and whatever DID
# land made `handoff_count() > 0` true, so the NEXT rebuild hit the guard
# above and skipped entirely. The failed record's own creation file just
# sat in `drained/`, permanently un-retried.
def test_an_insert_failure_rolls_back_every_record_this_attempt_restored(
    tmp_path, spool_dir, monkeypatch,
):
    db = tmp_path / "r.db"
    s1 = Store(db)
    spool.write(h("good", created_at=1), spool_dir)
    spool.write(h("poison", created_at=2), spool_dir)
    spool.drain(s1, spool_dir)
    s1.close()
    db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    s2 = Store(db)
    real_create = s2.create_handoff

    def flaky_create(handoff, project_id):
        if handoff.id == "poison":
            raise Exception("simulated insert failure")
        return real_create(handoff, project_id)

    monkeypatch.setattr(s2, "create_handoff", flaky_create)

    stats = spool.rebuild_if_empty(s2, spool_dir)

    assert stats.failed == 1
    assert stats.drained == 0
    # Rolled back, not half-applied: "good" was created_at=1, inserted BEFORE
    # "poison" failed, and must not be left behind.
    assert s2.handoff_count() == 0
    s2.close()


def test_a_fixed_retry_after_a_rolled_back_rebuild_restores_everything(
    tmp_path, spool_dir, monkeypatch,
):
    """The empty-table guard is what makes a retry possible at all -- proving
    the rollback actually left it empty, not just that the stats say so."""
    db = tmp_path / "r.db"
    s1 = Store(db)
    spool.write(h("good", created_at=1), spool_dir)
    spool.write(h("poison", created_at=2), spool_dir)
    spool.drain(s1, spool_dir)
    s1.close()
    db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    s2 = Store(db)
    real_create = s2.create_handoff
    monkeypatch.setattr(
        s2, "create_handoff",
        lambda handoff, pid: (_ for _ in ()).throw(Exception("boom"))
        if handoff.id == "poison" else real_create(handoff, pid),
    )
    first = spool.rebuild_if_empty(s2, spool_dir)
    assert first.failed == 1
    monkeypatch.undo()  # the cause is "fixed": create_handoff works again

    second = spool.rebuild_if_empty(s2, spool_dir)

    assert second.drained == 2
    pid = demo_pid(s2)
    assert {r["id"] for r in s2.handoffs(pid)} == {"good", "poison"}
    s2.close()


def test_journal_status_writes_a_record_that_cannot_be_mistaken_for_a_handoff(
    spool_dir,
):
    p = spool.journal_status("h1", "consumed", 1700, spool_dir)
    assert p == spool_dir / "drained" / "h1.1700.status.json"
    assert p.name.endswith(spool.STATUS_SUFFIX)
    assert json.loads(p.read_text()) == {
        "handoff_id": "h1", "status": "consumed", "at": 1700,
    }
    # The live outbox is for handoffs the server has not seen; a status change is
    # not one, so nothing here is pending.
    assert spool.pending(spool_dir) == []


def test_a_consumed_handoff_replays_as_consumed_not_queued(tmp_path, spool_dir):
    """`launch → rm ~/.bridge/bridge.db → bridge index` must not re-offer it.

    Phase 2 journalled creations only, so a rebuild showed every handoff as
    queued again. That cost nothing while nothing consumed a handoff. Once ▶
    exists it puts a prompt you already ran back at the top of the dashboard,
    and the panel's most load-bearing signal starts lying.
    """
    db = tmp_path / "c.db"
    s1 = Store(db)
    spool.write(h("h1", created_at=1), spool_dir)
    spool.drain(s1, spool_dir)
    s1.set_handoff_status("h1", "consumed")
    spool.journal_status("h1", "consumed", 2000, spool_dir)
    s1.close()

    # Lose the database exactly as `rm ~/.bridge/bridge.db` would.
    db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    s2 = Store(db)
    stats = spool.rebuild_if_empty(s2, spool_dir)

    assert (stats.drained, stats.statuses, stats.bad) == (1, 1, 0)
    assert s2.get_handoff("h1")["status"] == "consumed"
    assert s2.queued_handoff(demo_pid(s2)) is None, (
        "the rebuild re-offered a prompt that had already been launched"
    )
    s2.close()


def test_status_records_replay_in_at_order_not_glob_order(store, spool_dir):
    """A superseded-then-consumed history must land where it actually ended."""
    spool.journal(h("h1", created_at=1), spool_dir)
    spool.journal_status("h1", "queued", 999, spool_dir)
    spool.journal_status("h1", "superseded", 1000, spool_dir)
    spool.journal_status("h1", "consumed", 1001, spool_dir)

    # Filenames sort lexicographically, so the three-digit epoch lands LAST and
    # replaying in glob order would end on 'queued'. Asserting that here is what
    # keeps the test from being vacuous: with glob order equal to `at` order it
    # would pass with the sort removed.
    globbed = [
        p.name for p in sorted((spool_dir / "drained").glob("*" + spool.STATUS_SUFFIX))
    ]
    assert globbed[-1] == "h1.999.status.json"

    stats = spool.rebuild_if_empty(store, spool_dir)

    assert stats.statuses == 3
    assert store.get_handoff("h1")["status"] == "consumed"
    assert store.queued_handoff(demo_pid(store)) is None


def test_rebuild_is_still_skipped_when_status_records_are_present(store, spool_dir):
    """The empty-table guard is what keeps recovery from becoming replay.

    Here the journal and the live table disagree — the journal says consumed, the
    row says queued — and the live row must win. Replaying statuses onto a
    non-empty table would let a routine index rewrite live state.
    """
    spool.write(h("h1"), spool_dir)
    spool.drain(store, spool_dir)
    spool.journal_status("h1", "consumed", 2000, spool_dir)

    stats = spool.rebuild_if_empty(store, spool_dir)

    assert (stats.skipped, stats.statuses, stats.drained) == (1, 0, 0)
    assert store.get_handoff("h1")["status"] == "queued"
    assert store.queued_handoff(demo_pid(store))["id"] == "h1"


def test_a_corrupt_status_record_is_quarantined_and_the_replay_continues(
    tmp_path, spool_dir
):
    db = tmp_path / "q.db"
    spool.journal(h("h1", created_at=1), spool_dir)
    spool.journal(h("h2", created_at=2), spool_dir)
    drained = spool_dir / "drained"
    (drained / "h1.500.status.json").write_text("{not json at all")
    (drained / "h2.600.status.json").write_text('{"handoff_id": "h2"}')
    spool.journal_status("h1", "consumed", 700, spool_dir)

    s = Store(db)
    stats = spool.rebuild_if_empty(s, spool_dir)

    assert (stats.drained, stats.statuses, stats.bad) == (2, 1, 2)
    assert (spool_dir / "bad" / "h1.500.status.json").exists()
    assert (spool_dir / "bad" / "h2.600.status.json").exists()
    assert not (drained / "h1.500.status.json").exists()
    # Both creations landed, and the one good status record still applied.
    pid = demo_pid(s)
    assert {r["id"] for r in s.handoffs(pid)} == {"h1", "h2"}
    assert s.get_handoff("h1")["status"] == "consumed"
    assert s.queued_handoff(pid)["id"] == "h2"
    s.close()


def test_quarantining_a_corrupt_drain_file_logs_a_warning_naming_it(
    store, spool_dir, caplog
):
    """Quarantine drops spool depth with no user-visible signal; the log is it."""
    spool.write(h("good1", created_at=1), spool_dir)
    (spool_dir / "broken.json").write_text("{not json at all")

    with caplog.at_level(logging.WARNING, logger="bridge.spool"):
        stats = spool.drain(store, spool_dir)

    # Behaviour preserved: the good handoff drained, only the bad one quarantined.
    assert (stats.drained, stats.bad) == (1, 1)
    assert (spool_dir / "bad" / "broken.json").exists()
    assert store.queued_handoff(demo_pid(store))["id"] == "good1"
    # ...and the operator now gets a warning that names the offending file.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("broken.json" in r.getMessage() for r in warnings)


def test_quarantining_a_corrupt_status_record_logs_a_warning_naming_it(
    tmp_path, spool_dir, caplog
):
    spool.journal(h("h1", created_at=1), spool_dir)
    (spool_dir / "drained" / "h1.500.status.json").write_text("{not json at all")

    s = Store(tmp_path / "q.db")
    with caplog.at_level(logging.WARNING, logger="bridge.spool"):
        stats = spool.rebuild_if_empty(s, spool_dir)

    # Behaviour preserved: the creation replayed, only the bad status quarantined.
    assert (stats.drained, stats.bad) == (1, 1)
    assert (spool_dir / "bad" / "h1.500.status.json").exists()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("h1.500.status.json" in r.getMessage() for r in warnings)
    s.close()


# --- record id safety --------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["../pwn", "a/b", "..", ".", "", "/etc/passwd", "x\\y", "a\x00b"]
)
def test_a_record_id_that_carries_path_meaning_is_refused(spool_dir, bad):
    """`_atomic_write` resolves `<stem>.json` against the spool directory, so an
    id holding a separator would land the file outside it. The id arrives over
    the wire on `POST /api/handoff`, so this is the chokepoint every writer --
    `write`, `journal`, `journal_status` -- has to share."""
    with pytest.raises(ValueError):
        spool.journal(h(bad), spool_dir)
    with pytest.raises(ValueError):
        spool.write(h(bad), spool_dir)
    with pytest.raises(ValueError):
        spool.journal_status(bad, "launched", 1000, spool_dir)


def test_a_refused_record_id_writes_nothing_anywhere(spool_dir, tmp_path):
    """The guard has to run *before* the temp file, or a rejected id still
    leaves a `.tmp` behind -- and a traversal id must not create the directory
    it points at either."""
    escape = tmp_path / "escape"
    with pytest.raises(ValueError):
        spool.journal(h(f"../../{escape.name}/pwn"), spool_dir)
    assert not escape.exists()
    assert not list(spool_dir.rglob("*")) or spool.pending(spool_dir) == []


def test_an_ordinary_non_uuid_id_still_journals(spool_dir):
    """The guard rules out path characters, not ids that merely aren't uuids --
    `deadbeef`-style ids predate it and must keep working."""
    written = spool.journal(h("deadbeef"), spool_dir)
    assert written.name == "deadbeef.json"
    assert json.loads(written.read_text())["id"] == "deadbeef"
