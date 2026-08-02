"""The scheduled-run journal. Mirrors `test_spool.py`; see it for the shape."""

import json
from pathlib import Path

import pytest

from bridge import schedspool
from bridge.models import ScheduledRun
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
