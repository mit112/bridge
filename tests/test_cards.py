import pytest

from bridge.cards import build_cards, sort_key
from bridge.config import load
from bridge.models import GitState, SessionRecord
from bridge.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "c.db")
    yield s
    s.close()


def add(store, path, name, sid, ended, tokens=10):
    pid = store.upsert_project(path, name)
    store.upsert_session(
        SessionRecord(session_id=sid, transcript_path=f"/t/{sid}",
                      project_path=path, title=f"work in {name}",
                      ended_at=ended, tokens_in=tokens, tokens_out=tokens),
        pid,
    )
    return pid


def test_card_carries_session_and_git(store, tmp_path):
    add(store, "/p/one", "one", "s1", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})
    cards = build_cards(store, cfg, probe_fn=lambda p: GitState(status="ok", branch="main"))
    assert len(cards) == 1
    assert cards[0].name == "one"
    assert cards[0].session.title == "work in one"
    assert cards[0].git.branch == "main"


def test_not_a_repo_is_never_stale(store, tmp_path):
    """~43% of real projects are not repos; they must not show the warning."""
    add(store, "/p/two", "two", "s2", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db", "stale_hours": 1})
    cards = build_cards(store, cfg, probe_fn=lambda p: GitState(status="not_a_repo"))
    assert cards[0].is_stale is False


def test_stale_when_uncommitted_older_than_threshold(store, tmp_path):
    add(store, "/p/three", "three", "s3", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db", "stale_hours": 12})
    old = GitState(status="ok", branch="main", dirty_count=47,
                   oldest_uncommitted_at=1)  # 1970
    assert build_cards(store, cfg, probe_fn=lambda p: old)[0].is_stale is True


def test_clean_repo_is_not_stale(store, tmp_path):
    add(store, "/p/four", "four", "s4", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})
    clean = GitState(status="ok", branch="main", dirty_count=0,
                     oldest_uncommitted_at=None)
    assert build_cards(store, cfg, probe_fn=lambda p: clean)[0].is_stale is False


def test_probe_failure_still_yields_a_card(store, tmp_path):
    """No probe failure may prevent rendering."""
    add(store, "/p/five", "five", "s5", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})

    def boom(_):
        raise RuntimeError("git exploded")

    cards = build_cards(store, cfg, probe_fn=boom)
    assert len(cards) == 1
    assert cards[0].git.status == "unavailable"


def test_stale_cards_sort_above_fresh(store, tmp_path):
    fresh = GitState(status="ok", branch="main")
    stale = GitState(status="ok", branch="main", dirty_count=9,
                     oldest_uncommitted_at=1)
    add(store, "/p/aaa", "aaa", "s6", "2026-07-30T10:00:00.000Z")
    add(store, "/p/zzz", "zzz", "s7", "2026-07-30T09:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})
    cards = build_cards(
        store, cfg, probe_fn=lambda p: stale if p == "/p/zzz" else fresh
    )
    assert cards[0].name == "zzz"  # stale wins over alphabetical and recency


def test_sort_key_rank_is_first_element(store, tmp_path):
    """Later phases prepend ranks; the contract is rank-first."""
    add(store, "/p/six", "six", "s8", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})
    card = build_cards(store, cfg, probe_fn=lambda p: GitState(status="ok"))[0]
    assert isinstance(sort_key(card)[0], int)
