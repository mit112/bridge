"""Assemble one Card per project and order them by actionability.

Rank -1 is the most demanding of attention. Phase 2 added queued handoffs above
rank 0 and Phase 4 added running sessions, each by shifting these values;
`sort_key` returning a rank-first tuple is the contract that made both a local
change.

A queued handoff still outranks a running session, and that ordering is not the
obvious one: a running session needs nothing from you, while a queued one is
waiting on you.
"""

from dataclasses import replace

from bridge import agents, gitprobe
from bridge.config import Config, ModelChoice
from bridge.models import AgentsState, Card, GitState, SessionRecord
from bridge.store import Store, now_epoch, to_epoch

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
    store: Store, cfg: Config, probe_fn=None, agents_fn=None, debouncer=None
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
    if debouncer is not None:
        live_state = replace(
            live_state, sessions=debouncer.apply(live_state.sessions, now)
        )
    project_rows = store.projects()
    live_by_path = agents.by_project(
        live_state, store.alias_map(), [row["path"] for row in project_rows]
    )

    for row in project_rows:
        try:
            git = probe_fn(row["path"])
        except Exception:  # noqa: BLE001 - a broken probe must not hide a card
            git = GitState(status="unavailable")

        if git.status == "ok":
            store.put_git_cache(row["id"], git, now)
        elif git.status == "unavailable":
            # Only `unavailable` is transient, and it deliberately does not
            # write: caching it would overwrite the good state this fallback
            # exists to return, so the first timeout would break the feature
            # permanently. `not_a_repo` neither reads nor writes and falls
            # through untouched, so a deleted repo reports honestly.
            cached = store.get_git_cache(row["id"])
            if cached is not None:
                git, probed_at = cached
                git = replace(git, cached_at=probed_at)

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
    """Rank first, then most-recent-first, then name.

    A queued handoff outranks dirty-and-stale: a card that already knows its next
    step is more actionable than one that only knows something is wrong.
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
    return (rank, live_rank, -(ended or 0), card.name.lower())
