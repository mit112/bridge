import json
import os
from pathlib import Path

import pytest

from bridge import spool
from bridge.models import Handoff
from bridge.store import Store

DEMO = "/Users/mitsheth/dev/demo"


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
    store.set_alias("/Users/mitsheth/Documents/projectX", "/Users/mitsheth/dev/projectX")
    spool.write(h("h1", project_path="/Users/mitsheth/Documents/projectX"), spool_dir)

    spool.drain(store, spool_dir)

    assert store.project_by_path("/Users/mitsheth/Documents/projectX") is None
    canonical = store.project_by_path("/Users/mitsheth/dev/projectX")
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
