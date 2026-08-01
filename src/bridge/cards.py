"""Assemble one Card per project and order them by actionability.

Rank 0 is the most demanding of attention. Phase 2 will add queued handoffs and
Phase 4 running sessions above rank 0 by shifting these values; `sort_key`
returning a rank-first tuple is the contract that makes that a local change.
"""

from dataclasses import replace

from bridge import gitprobe
from bridge.config import Config, ModelChoice
from bridge.models import Card, GitState, SessionRecord
from bridge.store import Store, now_epoch, to_epoch

RANK_HANDOFF = -1
RANK_STALE = 0
RANK_RECENT = 1
RANK_OTHER = 2

FIVE_HOURS = 5 * 3600
ONE_DAY = 24 * 3600


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


def build_cards(store: Store, cfg: Config, probe_fn=None) -> list[Card]:
    # Late-bound default: looked up at call time (not at def time) so tests
    # can monkeypatch `gitprobe.probe` and have callers that omit `probe_fn`
    # (e.g. the API layer) pick up the replacement.
    if probe_fn is None:
        probe_fn = gitprobe.probe
    now = now_epoch()
    cards: list[Card] = []

    for row in store.projects():
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
    elif card.is_stale:
        rank = RANK_STALE
    elif card.session is not None:
        rank = RANK_RECENT
    else:
        rank = RANK_OTHER
    ended = to_epoch(card.session.ended_at) if card.session else None
    return (rank, -(ended or 0), card.name.lower())
