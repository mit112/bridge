"""Stale-while-revalidate around the per-project git probe.

Every test builds its OWN `GitProbeCache`. The cache holds cross-request state
(a freshness window and a set of in-flight refreshes), so a shared instance
would leak one test's claims into the next, and the module-level alternative
would be invisible to every test that injects its own `probe_fn`.

Most tests inject `InlineExecutor` or `QueueingExecutor` rather than the real
pool: the interesting assertions are about WHEN a probe happens, and a fake
executor makes that a fact instead of a race. One test
(`test_the_default_executor_really_refreshes_in_the_background`) deliberately
uses the real default pool, because everything else here would stay green if
the default were wired to nothing at all.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from bridge import gitprobe
from bridge.api import create_app
from bridge.cards import GitProbeCache, build_cards
from bridge.config import load
from bridge.models import AgentsState, GitState
from bridge.overview import build_overview
from bridge.store import Store, now_epoch
from bridge.workspace import build_workspace

# Injected wherever a builder would otherwise poll liveness for real. Nothing
# here is about sessions; this keeps `agents.probe` out of the picture entirely.
QUIET = AgentsState(status="ok", sessions=[], source="test")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "g.db")
    yield s
    s.close()


def project(store, path="/p/one", name="one") -> int:
    return store.upsert_project(path, name)


class InlineExecutor:
    """Runs the refresh on the calling thread, at submit time.

    This is what makes the stale case assertable rather than racy: by the time
    `states()` returns, the refresh has definitively finished and written, so a
    RETURNED value that is still the old state can only mean the return did not
    wait for it.
    """

    def submit(self, fn, *args):
        fn(*args)


class QueueingExecutor:
    """Records submissions and never runs them, so a refresh stays in flight."""

    def __init__(self):
        self.submitted: list[tuple] = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))


def counting(state: GitState):
    """A `probe_fn` that records the paths it was asked about."""
    calls: list[str] = []

    def probe(path):
        calls.append(str(path))
        return state

    return probe, calls


# --- the three windows -------------------------------------------------------


def test_a_cold_project_blocks_on_a_probe_and_caches_what_it_got(store):
    pid = project(store)
    probe, calls = counting(GitState(status="ok", branch="main"))
    cache = GitProbeCache(store, executor=InlineExecutor())

    states = cache.states(probe, [(pid, "/p/one")], 1000)

    assert calls == ["/p/one"]
    assert states[0].branch == "main"
    assert store.get_git_cache(pid) == (GitState(status="ok", branch="main"), 1000)


def test_a_fresh_entry_is_served_without_probing_at_all(store):
    """Inside the freshness window there is no probe, not even a background one:
    a burst of navigation must cost exactly one probe, not one per page."""
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="cached"), 999)
    probe, calls = counting(GitState(status="ok", branch="probed"))
    cache = GitProbeCache(store, fresh_s=10, executor=InlineExecutor())

    states = cache.states(probe, [(pid, "/p/one")], 1000)

    assert calls == []
    assert states[0].branch == "cached"
    # A within-window hit is not a fossil, and `cached_at` is what the templates
    # key off to say "Indexed 4 minutes ago" instead of showing the live state.
    assert states[0].cached_at is None


def test_a_stale_entry_returns_the_old_state_and_refreshes_behind_it(store):
    """Both halves of stale-while-revalidate, with no sleep and no polling.

    `InlineExecutor` runs the refresh DURING `states()`, so the store already
    holds the new state by the time the call returns. The returned value being
    the old one is therefore proof that the response was not made to wait; had
    it waited, it would be "new" like the store is.
    """
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="old"), 900)
    cache = GitProbeCache(
        store, fresh_s=10, max_age_s=300, executor=InlineExecutor()
    )

    states = cache.states(
        lambda p: GitState(status="ok", branch="new"), [(pid, "/p/one")], 1000
    )

    assert states[0].branch == "old"
    assert states[0].cached_at is None
    assert store.get_git_cache(pid)[0].branch == "new"


def test_an_entry_past_max_age_blocks_for_a_fresh_probe(store):
    """Past the ceiling a served state is likelier a lie than a saving, so this
    is the one case that pays the probe rather than answering from the row."""
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="ancient"), 600)
    probe, calls = counting(GitState(status="ok", branch="now"))
    cache = GitProbeCache(
        store, fresh_s=10, max_age_s=300, executor=InlineExecutor()
    )

    states = cache.states(probe, [(pid, "/p/one")], 1000)      # age 400 > 300

    assert calls == ["/p/one"]
    assert states[0].branch == "now"
    assert store.get_git_cache(pid)[1] == 1000


# --- dedupe ------------------------------------------------------------------


def test_a_second_stale_call_does_not_schedule_a_second_refresh(store):
    """Three rapid navigations are three chances to schedule the same probe.

    `QueueingExecutor` never runs what it is handed, so the first refresh is
    still outstanding when the next two calls arrive -- exactly the window the
    claim exists to cover.
    """
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="old"), 900)
    executor = QueueingExecutor()
    cache = GitProbeCache(store, fresh_s=10, executor=executor)
    probe = lambda p: GitState(status="ok", branch="new")      # noqa: E731

    for tick in (1000, 1001, 1002):
        cache.states(probe, [(pid, "/p/one")], tick)

    assert len(executor.submitted) == 1


def test_a_finished_refresh_releases_its_claim(store):
    """Otherwise the dedupe is permanent: one refresh per project, ever."""
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="old"), 900)
    executor = QueueingExecutor()
    cache = GitProbeCache(store, fresh_s=10, executor=executor)
    probe = lambda p: GitState(status="ok", branch="new")      # noqa: E731

    cache.states(probe, [(pid, "/p/one")], 1000)
    fn, args = executor.submitted.pop()
    fn(*args)                                       # that refresh completes
    store.put_git_cache(pid, GitState(status="ok", branch="old"), 900)   # stale again

    cache.states(probe, [(pid, "/p/one")], 1000)

    assert len(executor.submitted) == 1


# --- a refresh must never make things worse ----------------------------------


def test_a_refresh_whose_probe_raises_leaves_the_good_state_intact(store):
    """And surfaces nothing: with `InlineExecutor` the refresh runs inside
    `states()`, so an exception escaping it would fail this call outright."""
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="good"), 900)
    cache = GitProbeCache(store, fresh_s=10, executor=InlineExecutor())

    def boom(path):
        raise OSError("git went away")

    states = cache.states(boom, [(pid, "/p/one")], 1000)

    assert states[0].branch == "good"
    assert store.get_git_cache(pid) == (GitState(status="ok", branch="good"), 900)


@pytest.mark.parametrize("status", ["unavailable", "not_a_repo"])
def test_a_refresh_never_writes_a_non_ok_state_over_a_good_one(store, status):
    """The cache keeps `build_cards`' failure semantics: only "ok" is written.
    A background refresh that wrote `unavailable` would destroy the very state
    the fallback exists to return, and permanently."""
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="good"), 900)
    cache = GitProbeCache(store, fresh_s=10, executor=InlineExecutor())

    cache.states(lambda p: GitState(status=status), [(pid, "/p/one")], 1000)

    assert store.get_git_cache(pid) == (GitState(status="ok", branch="good"), 900)


def test_a_blocking_probe_that_fails_falls_back_to_the_fossil_with_its_age(store):
    """The `unavailable` fallback survives the cache, and this is the ONE path
    that still sets `cached_at` -- here the template's "Indexed ... ago" is the
    truthful rendering, because no live state could be had."""
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="last-good"), 600)
    cache = GitProbeCache(
        store, fresh_s=10, max_age_s=300, executor=InlineExecutor()
    )

    states = cache.states(
        lambda p: GitState(status="unavailable"), [(pid, "/p/one")], 1000
    )

    assert states[0].branch == "last-good"
    assert states[0].cached_at == 600


# --- order -------------------------------------------------------------------


def test_states_come_back_aligned_to_the_rows_it_was_given(store):
    """`build_cards` zips these straight onto `store.projects()`, so a result in
    any other order would attach one project's branch to another's card. All
    three windows are mixed here precisely because they are served by two
    different code paths."""
    ids = [project(store, f"/p/{n}", n) for n in ("a", "b", "c")]
    store.put_git_cache(ids[0], GitState(status="ok", branch="a-fresh"), 1000)
    store.put_git_cache(ids[2], GitState(status="ok", branch="c-stale"), 900)
    # ids[1] has no row at all and is the one that must be probed.
    cache = GitProbeCache(store, fresh_s=10, executor=QueueingExecutor())
    rows = [(ids[0], "/p/a"), (ids[1], "/p/b"), (ids[2], "/p/c")]

    states = cache.states(
        lambda p: GitState(status="ok", branch=f"probed{p}"), rows, 1000
    )

    assert [s.branch for s in states] == ["a-fresh", "probed/p/b", "c-stale"]


# --- the real pool -----------------------------------------------------------


def test_the_default_executor_really_refreshes_in_the_background(store, monkeypatch):
    """The default `ThreadPoolExecutor`, synchronised on the write itself.

    Waiting on the probe would prove only that a thread started; the claim being
    made is that the row ends up refreshed, so the event fires from
    `put_git_cache`. `Event.wait` with a timeout, never a sleep-and-look loop.
    """
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="old"), 900)
    refreshed = threading.Event()
    real_put = store.put_git_cache

    def put(project_id, git, probed_at):
        real_put(project_id, git, probed_at)
        refreshed.set()

    monkeypatch.setattr(store, "put_git_cache", put)
    cache = GitProbeCache(store, fresh_s=10)        # its own real thread pool
    ran_on: list[threading.Thread] = []

    def probe(path):
        ran_on.append(threading.current_thread())
        return GitState(status="ok", branch="new")

    states = cache.states(probe, [(pid, "/p/one")], 1000)

    assert states[0].branch == "old"                # returned without waiting
    assert refreshed.wait(5), "the default executor never ran the refresh"
    assert store.get_git_cache(pid)[0].branch == "new"
    assert ran_on[0] is not threading.current_thread()


# --- through `build_cards` and through a route -------------------------------


def test_build_cards_with_a_cache_serves_a_fresh_row_without_probing(store, tmp_path):
    """`git_cache` is opt-in for exactly the reason `debouncer` is: passing none
    must leave `build_cards` behaving as it did before the cache existed, which
    is what every other test in this suite relies on."""
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="cached"), now_epoch())
    cfg = load({"db_path": tmp_path / "g.db"})
    probe, calls = counting(GitState(status="ok", branch="probed"))
    cache = GitProbeCache(store, fresh_s=10, executor=InlineExecutor())

    with_cache = build_cards(store, cfg, probe_fn=probe, git_cache=cache)
    assert calls == []
    assert with_cache[0].git.branch == "cached"

    without_cache = build_cards(store, cfg, probe_fn=probe)
    assert calls == ["/p/one"]
    assert without_cache[0].git.branch == "probed"
    assert store.get_git_cache(pid)[0].branch == "probed"


def test_build_cards_with_a_cache_does_not_re_date_the_row_it_served(store, tmp_path):
    """`states()` owns the write, so `build_cards` must not settle its results a
    second time. Writing a served stale state back would reset that row's age to
    now, the row would look fresh forever, and the refresh it just scheduled
    would be clobbered by the next page load -- a cache that never updates."""
    pid = project(store)
    cfg = load({"db_path": tmp_path / "g.db"})
    stamped = now_epoch() - 60
    store.put_git_cache(pid, GitState(status="ok", branch="old"), stamped)
    cache = GitProbeCache(store, fresh_s=10, executor=QueueingExecutor())

    build_cards(
        store, cfg, probe_fn=lambda p: GitState(status="ok", branch="new"),
        git_cache=cache,
    )

    assert store.get_git_cache(pid) == (GitState(status="ok", branch="old"), stamped)


def test_build_overview_forwards_the_cache_when_it_builds_its_own_cards(
    store, tmp_path
):
    """`/` threads `cards` in, so this forward is the belt rather than the
    braces: a caller that omits them must not silently drop back to a full
    uncached sweep."""
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="cached"), now_epoch())
    cfg = load({"db_path": tmp_path / "g.db", "spool_dir": tmp_path / "spool"})
    probe, calls = counting(GitState(status="ok", branch="probed"))
    cache = GitProbeCache(store, fresh_s=10, executor=InlineExecutor())

    build_overview(store, cfg, live_state=QUIET, probe_fn=probe, git_cache=cache)

    assert calls == []


def test_build_workspace_forwards_the_cache(store, tmp_path):
    """With an explicit `probe_fn` this page probes like any other, so the
    forward is what decides whether it reads the window or sweeps git."""
    pid = project(store)
    store.put_git_cache(pid, GitState(status="ok", branch="cached"), now_epoch())
    cfg = load({"db_path": tmp_path / "g.db"})
    probe, calls = counting(GitState(status="ok", branch="probed"))
    cache = GitProbeCache(store, fresh_s=10, executor=InlineExecutor())

    model = build_workspace(
        store, cfg, pid, "current", live_state=QUIET, probe_fn=probe, git_cache=cache,
    )

    assert calls == []
    assert model.card.git.branch == "cached"


def test_two_page_loads_inside_the_window_probe_git_once(tmp_path, monkeypatch):
    """The wiring, through the app's own cache rather than an injected one.

    Every test above builds its own instance, so all of them would stay green
    with `create_app` wiring nothing. This is the one that fails if a route
    stops passing `git_cache` -- and `/projects` is included because it reaches
    `build_cards` down a different path than `/` does.
    """
    probe, calls = counting(GitState(status="ok", branch="main"))
    monkeypatch.setattr(gitprobe, "probe", lambda path, timeout=2.0: probe(path))
    cfg = load({"db_path": tmp_path / "r.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    store.upsert_project("/p/route", "route")
    client = TestClient(create_app(store, cfg))

    assert client.get("/").status_code == 200
    assert calls == ["/p/route"]                 # cold: one probe, then cached

    assert client.get("/").status_code == 200
    assert calls == ["/p/route"]
    assert client.get("/projects").status_code == 200
    assert calls == ["/p/route"]

    store.close()


def test_the_live_snapshot_does_not_re_probe_what_the_page_just_cached(
    tmp_path, monkeypatch
):
    """`/events`' opening frame, and every frame it sends on a new reindex
    generation, is a full `DashboardBuilder.full_update` with no cards -- so it
    is the most frequent git sweep in the app, once every 15s for as long as a
    panel stays open. It reads the same window the page does."""
    probe, calls = counting(GitState(status="ok", branch="main"))
    monkeypatch.setattr(gitprobe, "probe", lambda path, timeout=2.0: probe(path))
    cfg = load({"db_path": tmp_path / "e.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    store.upsert_project("/p/stream", "stream")
    client = TestClient(create_app(store, cfg))

    assert client.get("/").status_code == 200
    assert calls == ["/p/stream"]

    with client.stream("GET", "/events?max_ticks=1&interval=0") as response:
        assert response.status_code == 200
        "".join(response.iter_text())

    assert calls == ["/p/stream"]

    store.close()
