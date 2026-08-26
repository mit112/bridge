"""The scheduled-run journal. Mirrors `test_spool.py`; see it for the shape."""

import json
import logging
from pathlib import Path

import pytest

from bridge import schedspool
from bridge.models import ScheduledRun
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


def job(jid="j1", **kw):
    fields = dict(
        id=jid,
        project_path=DEMO,
        prompt="do the thing",
        mode="background",
        scheduled_for=2_000_000_000,
        created_at=1_000_000_000,
    )
    fields.update(kw)
    return ScheduledRun(**fields)


def test_journal_writes_one_readable_record_named_for_the_job(spool_dir):
    path = schedspool.journal(job(), spool_dir)

    assert path == spool_dir / "schedules" / "j1.json"
    assert json.loads(path.read_text())["prompt"] == "do the thing"


def test_journal_writes_beside_the_handoff_journal_not_into_it(spool_dir):
    schedspool.journal(job(), spool_dir)

    # `spool.rebuild_if_empty` globs `drained/*.json` and parses each via
    # `spool._load` as a Handoff, so a
    # schedule record landing there would be quarantined as a corrupt handoff.
    assert not (spool_dir / "drained").exists()


def test_re_journalling_the_same_id_overwrites_rather_than_accumulates(spool_dir):
    schedspool.journal(job(), spool_dir)
    schedspool.journal(job(prompt="edited"), spool_dir)

    files = sorted((spool_dir / "schedules").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["prompt"] == "edited"


def test_journal_status_writes_a_distinguishable_record(spool_dir):
    path = schedspool.journal_status("j1", "fired", 1700, spool_dir)

    assert path.name == "j1.1700.status.json"
    assert path.name.endswith(schedspool.STATUS_SUFFIX)
    assert json.loads(path.read_text()) == {
        "run_id": "j1", "status": "fired", "at": 1700
    }


def test_a_creation_record_missing_a_required_field_will_not_load(spool_dir):
    path = schedspool.journal(job(), spool_dir)
    path.write_text(json.dumps({"id": "j1"}))

    with pytest.raises(Exception):
        schedspool._load(path)


def test_a_status_record_without_an_integer_at_will_not_load(spool_dir):
    path = schedspool.journal_status("j1", "fired", 1700, spool_dir)
    path.write_text(json.dumps({"run_id": "j1", "status": "fired", "at": "soon"}))

    with pytest.raises(Exception):
        schedspool._load_status(path)


def test_an_unknown_key_is_ignored_so_a_newer_bridge_still_replays(spool_dir):
    path = schedspool.journal(job(), spool_dir)
    data = json.loads(path.read_text())
    data["invented_later"] = True
    path.write_text(json.dumps(data))

    assert schedspool._load(path).id == "j1"


def test_the_guard_rejects_a_write_to_the_real_bridge_dir():
    """The autouse conftest guard must cover this module's writers.

    `RealBridgeDirTouched` derives from BaseException, so this cannot be caught
    with `pytest.raises(Exception)`.
    """
    from tests.conftest import RealBridgeDirTouched

    with pytest.raises(RealBridgeDirTouched):
        schedspool.journal(job(), Path.home() / ".bridge" / "spool")


NOW = 1_500_000_000
PAST = NOW - 3600
FUTURE = NOW + 3600


def test_a_future_pending_job_replays_as_pending(store, spool_dir):
    schedspool.journal(job("j1", scheduled_for=FUTURE), spool_dir)

    stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert stats.restored == 1
    assert store.get_scheduled_run("j1")["status"] == "pending"


def test_a_fired_job_replays_as_fired_not_pending(store, spool_dir):
    schedspool.journal(job("j1", scheduled_for=PAST), spool_dir)
    schedspool.journal_status("j1", "fired", PAST + 1, spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("j1")["status"] == "fired"


def test_a_future_job_claimed_by_run_now_replays_as_indeterminate(store, spool_dir):
    """The duplicate-launch regression.

    `claim_specific` has no `scheduled_for` guard, so run-now can fire a job
    scheduled for tomorrow. If the database dies before the outcome is recorded,
    treating the creation record alone as `pending` would let the scheduler fire
    the same job again tomorrow. The claim record is what prevents that.
    """
    schedspool.journal(job("j1", scheduled_for=FUTURE), spool_dir)
    schedspool.journal_status("j1", "launching", NOW, spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("j1")["status"] == "indeterminate"


def test_a_replayed_terminal_row_is_reapable_by_retention(store, spool_dir):
    """`completed_at` NULL would make the row invisible to prune forever."""
    schedspool.journal(job("j1", scheduled_for=PAST), spool_dir)
    schedspool.journal_status("j1", "fired", PAST + 1, spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    # Assert the column, not a prune call: `prune_scheduled_runs`'s signature
    # changes in Task 3, and `completed_at` is the actual invariant.
    assert store.get_scheduled_run("j1")["completed_at"] == PAST + 1


def test_a_status_outside_the_vocabulary_is_quarantined_not_applied(store, spool_dir):
    """A record claiming `pending` would restore a fireable job."""
    schedspool.journal(job("j1", scheduled_for=PAST), spool_dir)
    forged = spool_dir / "schedules" / "j1.1700.status.json"
    forged.write_text(json.dumps({"run_id": "j1", "status": "pending", "at": 1700}))

    stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert stats.bad == 1
    assert store.get_scheduled_run("j1")["status"] == "missed"


def test_a_pruned_job_does_not_come_back_and_a_past_due_one_is_missed(
    store, spool_dir
):
    """The load-bearing test. All four replay rules in one scenario, against a
    database deleted the way `rm` deletes it."""
    schedspool.journal(job("future", scheduled_for=FUTURE), spool_dir)
    schedspool.journal(job("done", scheduled_for=PAST), spool_dir)
    schedspool.journal_status("done", "fired", PAST + 1, spool_dir)
    schedspool.journal(job("stale", scheduled_for=PAST), spool_dir)
    schedspool.journal(job("reaped", scheduled_for=PAST), spool_dir)
    schedspool.journal_status("reaped", "fired", PAST + 1, spool_dir)
    schedspool.journal_status("reaped", "pruned", PAST + 2, spool_dir)

    stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("future")["status"] == "pending"
    assert store.get_scheduled_run("done")["status"] == "fired"
    assert store.get_scheduled_run("stale")["status"] == "missed"
    assert store.get_scheduled_run("reaped") is None
    assert (stats.restored, stats.missed, stats.skipped_pruned) == (3, 1, 1)


def test_replay_is_skipped_when_the_table_is_not_empty(store, spool_dir):
    """Unguarded replay would resurrect finished jobs on every `bridge index`."""
    store.create_scheduled_run(job("already", scheduled_for=FUTURE))
    schedspool.journal(job("j1", scheduled_for=FUTURE), spool_dir)

    stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert stats.skipped == 1
    assert stats.restored == 0
    assert store.get_scheduled_run("j1") is None


def test_the_latest_status_wins_regardless_of_glob_order(store, spool_dir):
    schedspool.journal(job("j1", scheduled_for=PAST), spool_dir)
    schedspool.journal_status("j1", "failed", PAST + 9, spool_dir)
    schedspool.journal_status("j1", "fired", PAST + 1, spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("j1")["status"] == "failed"


def test_an_edited_prompt_is_what_replays(store, spool_dir):
    schedspool.journal(job("j1", scheduled_for=FUTURE), spool_dir)
    schedspool.journal(job("j1", scheduled_for=FUTURE, prompt="edited"), spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("j1")["prompt"] == "edited"


def test_a_corrupt_record_is_quarantined_and_the_replay_continues(store, spool_dir):
    schedspool.journal(job("good", scheduled_for=FUTURE), spool_dir)
    bad = spool_dir / "schedules" / "bad.json"
    bad.write_text("{not json")

    stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert stats.bad == 1
    assert store.get_scheduled_run("good")["status"] == "pending"
    assert (spool_dir / "bad" / "bad.json").exists()


def test_quarantining_a_corrupt_schedule_record_logs_a_warning_naming_it(
    store, spool_dir, caplog
):
    schedspool.journal(job("good", scheduled_for=FUTURE), spool_dir)
    (spool_dir / "schedules" / "bad.json").write_text("{not json")

    with caplog.at_level(logging.WARNING, logger="bridge.schedspool"):
        stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    # Behaviour preserved: the good creation replayed, only the bad one quarantined.
    assert stats.bad == 1
    assert store.get_scheduled_run("good")["status"] == "pending"
    assert (spool_dir / "bad" / "bad.json").exists()
    # ...and the operator now gets a warning that names the offending file.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("bad.json" in r.getMessage() for r in warnings)


def test_quarantining_a_corrupt_status_record_logs_a_warning_naming_it(
    store, spool_dir, caplog
):
    schedspool.journal(job("good", scheduled_for=FUTURE), spool_dir)
    (spool_dir / "schedules" / "good.500.status.json").write_text("{not json")

    with caplog.at_level(logging.WARNING, logger="bridge.schedspool"):
        stats = schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert stats.bad == 1
    assert store.get_scheduled_run("good")["status"] == "pending"
    assert (spool_dir / "bad" / "good.500.status.json").exists()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("good.500.status.json" in r.getMessage() for r in warnings)


def test_the_journal_survives_a_real_database_deletion(tmp_path, spool_dir):
    """`rm ~/.bridge/bridge.db` is the scenario this whole module exists for."""
    db = tmp_path / "db" / "s.db"
    store = Store(db)
    j = job("j1", scheduled_for=FUTURE)
    schedspool.journal(j, spool_dir)
    store.create_scheduled_run(j)
    store.close()

    db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    rebuilt = Store(db)
    try:
        stats = schedspool.rebuild_if_empty(rebuilt, spool_dir, now=NOW)
        assert stats.restored == 1
        assert rebuilt.get_scheduled_run("j1")["prompt"] == "do the thing"
    finally:
        rebuilt.close()


def test_a_replayed_retry_keeps_its_provenance(store, spool_dir):
    """A NULL `retry_of` lets `retry_terminal` grant a second retry."""
    schedspool.journal(job("r1", scheduled_for=PAST, retry_of="orig"), spool_dir)
    schedspool.journal_status("r1", "fired", PAST + 1, spool_dir)

    schedspool.rebuild_if_empty(store, spool_dir, now=NOW)

    assert store.get_scheduled_run("r1")["retry_of"] == "orig"
