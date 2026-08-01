import pytest

from bridge.cards import build_cards, model_options, sort_key
from bridge.config import ModelChoice, load
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
    """~43% of real project paths are not repos and must never show the warning.

    The fixture is deliberately dirty AND ancient, so `_is_stale` returning False
    can ONLY be due to the status check. With defaults (dirty_count=0,
    oldest_uncommitted_at=None) this test passes even without that check.
    """
    add(store, "/p/two", "two", "s2", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db", "stale_hours": 1})
    not_repo = GitState(status="not_a_repo", dirty_count=47, oldest_uncommitted_at=1)
    cards = build_cards(store, cfg, probe_fn=lambda p: not_repo)
    assert cards[0].is_stale is False


def test_unavailable_git_is_never_stale(store, tmp_path):
    """Same reasoning for a failed probe: no data means no warning."""
    add(store, "/p/three", "three", "s3", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db", "stale_hours": 1})
    unavail = GitState(status="unavailable", dirty_count=47, oldest_uncommitted_at=1)
    cards = build_cards(store, cfg, probe_fn=lambda p: unavail)
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


# --- Phase 2: queued handoffs outrank everything else ------------------------


def test_a_queued_handoff_sorts_above_a_dirty_and_stale_project(tmp_path):
    """A card that already knows its next step is more actionable than one that
    only knows something is wrong."""
    from bridge.models import Handoff

    cfg = load({"db_path": tmp_path / "sort.db",
                "spool_dir": tmp_path / "spool", "stale_hours": 1})
    store = Store(cfg.db_path)

    stale_pid = store.upsert_project("/Users/mitsheth/dev/aaa-stale", "aaa-stale")
    queued_pid = store.upsert_project("/Users/mitsheth/dev/zzz-queued", "zzz-queued")
    for pid, sid in ((stale_pid, "s-stale"), (queued_pid, "s-queued")):
        store.upsert_session(
            SessionRecord(session_id=sid, transcript_path=f"/t/{sid}",
                          title="work", ended_at="2026-07-30T10:00:00.000Z"),
            pid,
        )
    store.create_handoff(
        Handoff(id="h1", project_path="/Users/mitsheth/dev/zzz-queued",
                next_prompt="do the next thing", summary="a summary",
                created_at=1000),
        queued_pid,
    )

    # The stale one is dirty and old; the queued one is a clean repo.
    def probe(path):
        if path.endswith("aaa-stale"):
            return GitState(status="ok", branch="main", dirty_count=9,
                            oldest_uncommitted_at=1)
        return GitState(status="ok", branch="main", dirty_count=0)

    cards = build_cards(store, cfg, probe_fn=probe)
    store.close()

    assert [c.name for c in cards] == ["zzz-queued", "aaa-stale"], (
        "the queued handoff must outrank dirty-and-stale, and must not be "
        "decided by name order"
    )
    assert cards[0].handoff["summary"] == "a summary"
    assert cards[1].handoff is None


# --- Phase 3: the launcher ---------------------------------------------------


def test_a_card_carries_the_configured_launch_options(store, tmp_path):
    """The launch band's selects render per card, so the card carries the lists.

    `xhigh` and `max` are asserted explicitly: `claude --effort` accepts them and
    Phase 3 is the first phase to surface the list, so a shortened list would
    silently make two valid efforts unreachable from the panel.
    """
    add(store, "/p/opts", "opts", "s-opts", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})
    card = build_cards(store, cfg, probe_fn=lambda p: GitState(status="ok"))[0]

    assert card.launch_models == cfg.models
    assert card.launch_efforts == cfg.efforts
    assert card.launch_efforts == ["low", "medium", "high", "xhigh", "max"]


# --- Phase 4 Task 1: the model catalog ---------------------------------------


def test_an_off_catalog_suggestion_is_prepended_labelled_as_itself():
    """Silently launching a different model than the last session used is worse
    than showing an unfamiliar value, so an unknown suggestion is surfaced."""
    catalog = [ModelChoice("opus", "opus — latest"), ModelChoice("sonnet", "sonnet")]
    options = model_options(catalog, "claude-opus-4-2")
    assert options[0] == ModelChoice("claude-opus-4-2", "claude-opus-4-2")
    assert len(options) == 3


def test_a_suggestion_already_in_the_catalog_is_not_duplicated():
    catalog = [ModelChoice("opus", "opus — latest"), ModelChoice("sonnet", "sonnet")]
    options = model_options(catalog, "sonnet")
    assert [m.value for m in options] == ["opus", "sonnet"]


def test_no_suggestion_leaves_the_catalog_untouched():
    catalog = [ModelChoice("opus", "opus — latest")]
    assert model_options(catalog, None) == catalog


def test_model_options_does_not_alias_the_caller_s_catalog():
    """`build_cards` hands this the Config's list once per card; mutating it
    would leak across cards and grow the catalog on every page load.

    Exercised with NO suggestion on purpose. The prepend branch builds a fresh
    list anyway, so passing an off-catalog suggestion here would test the wrong
    path and let `return catalog` survive.
    """
    catalog = [ModelChoice("opus", "opus — latest")]
    model_options(catalog, None).append(ModelChoice("x", "x"))
    assert len(catalog) == 1
    # And the in-catalog-suggestion path, which returns by the same statement.
    model_options(catalog, "opus").append(ModelChoice("y", "y"))
    assert len(catalog) == 1


def test_the_card_carries_model_choices_not_bare_strings(store, tmp_path):
    """The template reads `.value` and `.label`; a plain str has neither."""
    add(store, "/p/cat", "cat", "s-cat", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})
    card = build_cards(store, cfg, probe_fn=lambda p: GitState(status="ok"))[0]
    assert all(isinstance(m, ModelChoice) for m in card.launch_models)
