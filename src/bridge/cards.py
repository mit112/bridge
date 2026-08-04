"""Assemble one Card per project and order them by actionability.

Rank -1 is the most demanding of attention. Phase 2 added queued handoffs above
rank 0 and Phase 4 added running sessions, each by shifting these values;
`sort_key` returning a rank-first tuple is the contract that made both a local
change.

A queued handoff still outranks a running session, and that ordering is not the
obvious one: a running session needs nothing from you, while a queued one is
waiting on you.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from bridge import agents, gitprobe, hooks
from bridge.config import Config, ModelChoice
from bridge.models import AgentsState, Card, GitState, SessionRecord
from bridge.store import Store, now_epoch, to_epoch


def _probe_one(probe_fn, path) -> GitState:
    try:
        return probe_fn(path)
    except Exception:  # noqa: BLE001 - a broken probe must not hide a card
        return GitState(status="unavailable")


def _probe_all(probe_fn, paths) -> list[GitState]:
    """Probe every project's working tree at once instead of one after another.

    This is the whole cost of rendering `/` and `/projects`. `gitprobe.probe`
    shells out to git up to five times per project, so a 36-project index was
    spawning ~180 subprocesses strictly in series and taking 1.2-1.8s per page
    load, against ~1ms for `/schedule`, which reads no working trees.

    Threads rather than processes because every one of those probes is a
    `subprocess.run` that spends its life blocked on a child -- the GIL is
    released for the whole wait, so this parallelises cleanly and adds no
    pickling cost. The pool is capped so a machine with hundreds of indexed
    projects does not try to fork hundreds of gits at once; git is disk-bound
    and past a point more concurrency just thrashes. 16 is where the win
    plateaus here: measured 36 projects at 1.42s serial -> 0.38s at 16 workers
    -> 0.35s at 32, so the extra threads buy nothing and only widen the fork
    storm on a bigger index.

    `pool.map` preserves input order, so the caller can `zip` these straight
    back onto `project_rows` -- the card order stays exactly what
    `store.projects()` returned. The probes are the ONLY part parallelised:
    the loop that consumes them touches `store`, and this app's SQLite
    connection is single-threaded, so it deliberately stays that way.
    """
    if len(paths) < 2:
        return [_probe_one(probe_fn, p) for p in paths]
    with ThreadPoolExecutor(max_workers=min(16, len(paths))) as pool:
        return list(pool.map(lambda p: _probe_one(probe_fn, p), paths))


def _settle_cache(store: Store, project_id: int, git: GitState, now: int) -> GitState:
    """Write a good probe through to the cache, or fall back to the last one.

    Only `unavailable` is transient, and it deliberately does not write:
    caching it would overwrite the good state this fallback exists to return,
    so the first timeout would break the feature permanently. `not_a_repo`
    neither reads nor writes and falls through untouched, so a deleted repo
    reports honestly.
    """
    if git.status == "ok":
        store.put_git_cache(project_id, git, now)
    elif git.status == "unavailable":
        cached = store.get_git_cache(project_id)
        if cached is not None:
            state, probed_at = cached
            return replace(state, cached_at=probed_at)
    return git


RANK_HANDOFF = -1
RANK_RUNNING = 0
RANK_STALE = 1
RANK_RECENT = 2
RANK_OTHER = 3

# muxara's ladder, including its deliberate rank collision between Idle and
# Unknown: equal-priority rows then hold a stable secondary sort instead of
# reshuffling on every poll.
LIVE_PRIORITY = {
    "needs_input": 0,
    "blocked": 0,   # a background agent waiting on something is the same call
    "failed": 1,
    "errored": 1,
    "busy": 2,
    "working": 2,
    "idle": 3,
}
LIVE_PRIORITY_DEFAULT = 3   # unknown ranks WITH idle, not above it

# `ccmanager`'s IDLE_DEBOUNCE_MS after a 500 -> 1000 -> 1500 ladder across four
# issues, and Conductor's RUNTIME_STATUS_CACHE_TTL, converged independently on
# the same number. Deliberately NOT muxara's 300 s cool-off: that exists to
# paper over terminal scraping, which Bridge does not do.
IDLE_DEBOUNCE_S = 1.5

FIVE_HOURS = 5 * 3600
ONE_DAY = 24 * 3600
SPARK_DAYS = 7


def live_priority(status: str) -> int:
    """Where a live status sits on the attention ladder."""
    return LIVE_PRIORITY.get(status, LIVE_PRIORITY_DEFAULT)


class LivenessDebouncer:
    """Damp the busy -> idle transition, and only that one.

    A session that goes quiet for a single sample is usually still working, and
    a card that flickers between "running" and "idle" is worse than one that
    lags by a second and a half. Becoming busy is adopted instantly: the point
    is to avoid claiming quiescence too early, never to delay showing work.

    Debounced server-side, BEFORE anything is emitted, so a flapping
    classification never reaches a client as an event at all. `now` is injected
    so no test has to sleep for real time.
    """

    def __init__(self, hold_s: float = IDLE_DEBOUNCE_S):
        self._hold = hold_s
        self._shown: dict[str, str] = {}        # session_id -> what we last showed
        self._quiet_since: dict[str, float] = {}

    def apply(self, sessions: list, now: float) -> list:
        out = []
        live_ids = set()
        for session in sessions:
            live_ids.add(session.session_id)
            out.append(self._settle(session, now))
        # A session that is gone cannot flap, and keeping its entry would leak
        # one dict slot per session ever seen.
        for gone in set(self._shown) - live_ids:
            self._shown.pop(gone, None)
            self._quiet_since.pop(gone, None)
        return out

    def _settle(self, session, now: float):
        sid, reported = session.session_id, session.status
        shown = self._shown.get(sid)

        if reported == "idle" and shown == "busy":
            quiet_since = self._quiet_since.setdefault(sid, now)
            if now - quiet_since < self._hold:
                # Still inside the hold, so keep SHOWING busy even though the
                # sensor says idle. Returning `session` here would emit the
                # idle it is the whole job of this branch to withhold.
                self._shown[sid] = "busy"
                return replace(session, status="busy")

        self._quiet_since.pop(sid, None)
        self._shown[sid] = reported
        return session


class GitProbeCache:
    """Serve the last known git state now; refresh it behind the request.

    `_probe_all` made the per-request probe fast, not absent: `/` and
    `/projects` still spend ~350ms of their TTFB shelling out to git for every
    project before a byte is written. Expensive I/O inside the request handler
    is the shape being removed here, not the number of threads doing it.

    Three windows, off each project's `git_cache` row:

      * age <= `fresh_s`         serve the row, probe nothing at all
      * `fresh_s` .. `max_age_s` serve the row IMMEDIATELY and refresh behind
                                 the response, so the next look is current
      * older, or no row at all  block on a probe; there is nothing honest yet

    `fresh_s=10` is sized to a burst of navigation. Moving between `/`,
    `/projects` and a project page is 1-5s of wall clock and git facts do not
    change in that time, so a burst of clicks costs exactly one probe. It stays
    just inside `RefreshCoordinator.interval_s` (15s), the cadence at which the
    panel's own data turns over: a freshness window wider than that would serve
    states the rest of the page has already moved past.

    `max_age_s=300` is where serving stops beating being right. Five minutes is
    long enough to change branch and commit, so a row that old is likelier a
    lie than a saving, and paying `_probe_all`'s 0.38s once is better than
    rendering the wrong branch. Reaching the ceiling means nobody has opened the
    panel in five minutes, so the cost lands on the first look after a long gap
    and every look after that one is instant.

    A stale hit is deliberately NOT marked `cached_at`. That field means "the
    probe failed and this is a fossil" -- the templates render it instead of the
    live state, as "Indexed 4 minutes ago" -- and a within-window revalidating
    hit is a different claim. Only `_settle_cache`'s `unavailable` fallback
    still sets it.

    Writing from the refresh thread is safe because `Store` guards every
    statement with an `RLock` over a `check_same_thread=False` connection: the
    refresh serialises against the request rather than racing it.
    """

    def __init__(self, store: Store, fresh_s: int = 10, max_age_s: int = 300,
                 executor=None):
        self._store = store
        self._fresh_s = fresh_s
        self._max_age_s = max_age_s
        # Four workers, not `_probe_all`'s sixteen: a refresh is off the
        # critical path, so it may take as long as it likes, and a narrow pool
        # is what stops a background sweep from competing for disk with the
        # blocking probes of whatever request is in flight. Injectable so a
        # test can run the refresh inline instead of waiting on a thread.
        self._executor = executor or ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="git-refresh"
        )
        self._lock = threading.Lock()
        self._refreshing: set[int] = set()

    def states(
        self, probe_fn, rows: list[tuple[int, str]], now: int
    ) -> list[GitState]:
        """One `GitState` per row, in the order the rows were given."""
        out: list[GitState] = [None] * len(rows)        # type: ignore[list-item]
        blocking: list[int] = []
        for i, (project_id, path) in enumerate(rows):
            cached = self._store.get_git_cache(project_id)
            if cached is None:
                blocking.append(i)
                continue
            state, probed_at = cached
            age = now - probed_at
            if age > self._max_age_s:
                blocking.append(i)
                continue
            # Filled in BEFORE the refresh is scheduled, so even an executor
            # that runs its work inline cannot make this call return the
            # refreshed value -- the whole promise here is that the response
            # does not wait.
            out[i] = state
            if age > self._fresh_s:
                self._schedule(probe_fn, project_id, path)
        if blocking:
            # Still one `_probe_all`: the projects that do have to be probed are
            # probed together rather than one after another.
            probed = _probe_all(probe_fn, [rows[i][1] for i in blocking])
            for i, git in zip(blocking, probed):
                out[i] = _settle_cache(self._store, rows[i][0], git, now)
        return out

    def _schedule(self, probe_fn, project_id: int, path: str) -> None:
        """At most one outstanding refresh per project.

        Three quick navigations inside the stale window are three chances to
        schedule the same probe; without this claim the cache would spend MORE
        subprocesses than the uncached build it replaces.
        """
        with self._lock:
            if project_id in self._refreshing:
                return
            self._refreshing.add(project_id)
        # Submitted outside the lock: an injected inline executor runs the
        # refresh right here, and the refresh takes this same lock to release
        # the claim.
        self._executor.submit(self._refresh, probe_fn, project_id, path)

    def _refresh(self, probe_fn, project_id: int, path: str) -> None:
        try:
            git = _probe_one(probe_fn, path)
            # `_probe_one` turns a raising probe into `unavailable`, and only
            # "ok" is ever written, so a refresh that fails leaves the good
            # state it was trying to replace exactly where it was. Nothing
            # reports the failure: the only consequence is that the next look
            # is stale too, and it will schedule another one.
            if git.status == "ok":
                # `now_epoch()` and not the request's `now`: by the time a
                # refresh lands, the request that scheduled it may be seconds
                # gone, and dating the row from then would shorten its own
                # freshness window.
                self._store.put_git_cache(project_id, git, now_epoch())
        finally:
            # Released even if the write itself raised, or one bad call would
            # wedge this project's refreshes for the life of the process.
            with self._lock:
                self._refreshing.discard(project_id)


def spark_points(values: list[int], width: int = 72, height: int = 20) -> str:
    """SVG polyline points for a token-burn sparkline.

    Flat series (including all-zero, the common idle case) render at the
    baseline rather than dividing by a zero range, and a one-point series does
    not divide by a zero x-step either -- a `days=1` window reaches both.
    """
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo
    step = width / (len(values) - 1) if len(values) > 1 else 0.0
    out = []
    for i, v in enumerate(values):
        # y is inverted: SVG's origin is top-left, so a peak is y=0 and the
        # trough sits at y=height.
        y = height - (v - lo) / span * height if span else height
        out.append(f"{i * step:.1f},{y:.1f}")
    return " ".join(out)


def model_options(
    catalog: list[ModelChoice], suggested: str | None
) -> list[ModelChoice]:
    """The catalog, with an off-catalog suggestion prepended labelled as itself.

    Silently launching a different model than the last session used is worse
    than showing an unfamiliar value, so an unknown suggestion is surfaced
    rather than dropped. Always returns a new list: the caller passes the
    Config's own catalog, which must not be mutated by a card build.
    """
    if suggested and suggested not in [m.value for m in catalog]:
        return [ModelChoice(suggested, suggested), *catalog]
    return list(catalog)


def build_cards(
    store: Store, cfg: Config, probe_fn=None, agents_fn=None, debouncer=None,
    hook_state=None, git_cache=None,
) -> list[Card]:
    # Late-bound default: looked up at call time (not at def time) so tests
    # can monkeypatch `gitprobe.probe` and have callers that omit `probe_fn`
    # (e.g. the API layer) pick up the replacement.
    if probe_fn is None:
        probe_fn = gitprobe.probe
    if agents_fn is None:
        agents_fn = agents.probe
    now = now_epoch()
    cards: list[Card] = []

    # ONE liveness read for the whole build, before the loop. Probing per card
    # would mean N reads (and, with the subprocess sensor, N spawns) per page
    # load. Matching `probe_fn`: a broken sensor hides no cards.
    try:
        live_state = agents_fn()
    except Exception:  # noqa: BLE001
        live_state = AgentsState(status="unavailable", sessions=[], source="none")
    live_unavailable = live_state.status == "unavailable"

    # Hooks are an OVERLAY on the sensor, never a substitute for it. The sensor
    # decides what exists; a hook can only say that something the sensor can
    # already see is waiting on a human. `forget` is the reconciliation: a
    # session the sensor no longer reports cannot be waiting, whatever the last
    # hook said, and hook events are silently lost whenever Bridge is down.
    if hook_state is not None and live_state.status == "ok":
        hook_state.forget(s.session_id for s in live_state.sessions)
        waiting = hook_state.waiting_ids()
        if waiting:
            live_state = replace(live_state, sessions=[
                replace(s, status=hooks.NEEDS_INPUT)
                if s.session_id in waiting else s
                for s in live_state.sessions
            ])

    if debouncer is not None:
        live_state = replace(
            live_state, sessions=debouncer.apply(live_state.sessions, now)
        )
    project_rows = store.projects()
    live_by_path = agents.by_project(
        live_state, store.alias_map(), [row["path"] for row in project_rows]
    )

    # A `git_cache` is the caller's collaborator, like `debouncer`: with one, it
    # owns the read/write/fallback per project (its blocking probes go through
    # `_settle_cache` itself), so this build must not settle the results again.
    # With none, behaviour is exactly what it was before the cache existed.
    if git_cache is None:
        probed = _probe_all(probe_fn, [row["path"] for row in project_rows])
    else:
        probed = git_cache.states(
            probe_fn, [(row["id"], row["path"]) for row in project_rows], now
        )

    for row, git in zip(project_rows, probed):
        if git_cache is None:
            git = _settle_cache(store, row["id"], git, now)

        handoff = _handoff(store, row["id"])
        cards.append(
            Card(
                project_id=row["id"],
                path=row["path"],
                name=row["name"],
                session=_session(store, row["id"]),
                git=git,
                tokens_today=store.token_totals(row["id"], now - ONE_DAY),
                tokens_5h=store.token_totals(row["id"], now - FIVE_HOURS),
                spark=store.token_series(row["id"], SPARK_DAYS, now),
                is_stale=_is_stale(git, cfg.stale_hours, now),
                pinned=bool(row["pinned"]),
                handoff=handoff,
                # Resolved here rather than in Jinja: prepending an off-catalog
                # suggestion needs to construct a ModelChoice, and exposing the
                # class to the template environment to do that would put a data
                # decision inside the markup.
                launch_models=model_options(
                    cfg.models, (handoff or {}).get("suggested_model")
                ),
                launch_efforts=list(cfg.efforts),
                launch_permission_modes=list(cfg.permission_modes),
                live=(live_by_path.get(row["path"]) or [None])[0],
                # Distinct from `live is None`: "the sensor failed" and
                # "nothing is running here" must never render as the same
                # thing, or the panel asserts quiescence it never observed.
                live_unavailable=live_unavailable,
            )
        )

    cards.sort(key=sort_key)
    return cards


def _session(store: Store, project_id: int) -> SessionRecord | None:
    row = store.latest_session(project_id)
    if row is None:
        return None
    return SessionRecord(
        session_id=row["id"], transcript_path=row["transcript_path"] or "",
        title=row["title"], started_at=row["started_at"], ended_at=row["ended_at"],
        model=row["model"], effort=row["effort"], git_branch=row["git_branch"],
        user_msgs=row["user_msgs"], assistant_msgs=row["assistant_msgs"],
        last_prompt=row["last_prompt"], tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        tokens_cache_create=row["tokens_cache_create"],
        tokens_cache_read=row["tokens_cache_read"],
        sidechain_tokens=row["sidechain_tokens"],
        interrupted=bool(row["interrupted"]),
    )


def _handoff(store: Store, project_id: int) -> dict | None:
    row = store.queued_handoff(project_id)
    return dict(row) if row is not None else None


def _is_stale(git: GitState, stale_hours: int, now: int) -> bool:
    """Only a real repo with real uncommitted work can be stale."""
    if git.status != "ok" or git.dirty_count == 0 or git.oldest_uncommitted_at is None:
        return False
    return (now - git.oldest_uncommitted_at) > stale_hours * 3600


def sort_key(card: Card) -> tuple:
    """Pinned first, then rank, then most-recent-first, then name.

    Pin outranks everything, a queued handoff included. Every other term here is
    something Bridge inferred about a project; a pin is the one thing the user
    said outright, and an inference must not overrule an instruction. The cost is
    accepted knowingly: the top card is no longer guaranteed to be the one with a
    next step ready.

    Below that, a queued handoff outranks dirty-and-stale: a card that already
    knows its next step is more actionable than one that only knows something is
    wrong.
    """
    if card.handoff:
        rank = RANK_HANDOFF
    elif card.live is not None:
        rank = RANK_RUNNING
    elif card.is_stale:
        rank = RANK_STALE
    elif card.session is not None:
        rank = RANK_RECENT
    else:
        rank = RANK_OTHER
    # Within the running band, the attention ladder decides. Idle and unknown
    # collide deliberately, so those rows fall through to the stable
    # recency-then-name sort rather than reshuffling on every poll.
    live_rank = live_priority(card.live.status) if card.live else 0
    ended = to_epoch(card.session.ended_at) if card.session else None
    return (0 if card.pinned else 1, rank, live_rank, -(ended or 0),
            card.name.lower())
