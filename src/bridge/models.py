from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # `config` imports nothing from here, so this is cycle-free;
    # it stays behind the guard only to keep `models` importable on its own.
    from bridge.config import ModelChoice


@dataclass
class SessionRecord:
    session_id: str
    transcript_path: str
    project_path: str | None = None
    title: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    model: str | None = None
    effort: str | None = None
    git_branch: str | None = None
    user_msgs: int = 0
    assistant_msgs: int = 0
    last_prompt: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache_create: int = 0
    tokens_cache_read: int = 0
    sidechain_tokens: int = 0
    interrupted: bool = False
    # The last requestId whose usage was counted. Persisted, because an
    # incremental scan boundary can land between two entries of one response.
    last_usage_request_id: str | None = None


@dataclass
class GitState:
    """status is the discriminator: ok | not_a_repo | unavailable."""

    status: str
    branch: str | None = None
    dirty_count: int = 0
    ahead: int | None = None
    behind: int | None = None
    last_commit_summary: str | None = None
    last_commit_at: int | None = None
    oldest_uncommitted_at: int | None = None


@dataclass
class Handoff:
    """An authored next-session prompt.

    Unlike everything else Bridge stores, this cannot be regenerated from
    transcripts: it is composed once, at the end of a session, and if it is lost
    it is gone. `project_path` is the raw path the CLI observed; alias
    resolution to a canonical project happens server-side, never here.
    """

    id: str
    project_path: str
    next_prompt: str
    source_session_id: str | None = None
    summary: str | None = None
    suggested_model: str | None = None
    suggested_effort: str | None = None
    created_at: int = 0
    status: str = "queued"


@dataclass
class Launch:
    """One spawn attempt, recorded before anything is spawned.

    The row exists even when the spawn fails, which is what keeps a session
    correlatable in the case that needs it most. `handoff_id` is None for an
    ad-hoc prompt with no queued handoff behind it. `session_id` is None for a
    background launch, because `claude --bg` mints its own id and does not tell
    us until it has printed its handle. `short_id` is deliberately absent here:
    like `Handoff.consumed_at` it is stamped by the store — `set_launch_session`
    — and never authored by the caller.
    """

    id: str
    project_id: int
    mode: str
    prompt: str
    handoff_id: str | None = None
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    launched_at: int = 0
    outcome: str = "pending"


@dataclass
class Card:
    project_id: int
    path: str
    name: str
    session: SessionRecord | None
    git: GitState
    tokens_today: int
    tokens_5h: int
    spark: list[int] = field(default_factory=list)
    is_stale: bool = False
    # The queued handoff, as a plain dict. A card carries at most one: the store
    # supersedes the rest, so "what next" is never ambiguous.
    handoff: dict | None = None
    # The launch band's option lists, copied off `Config` by `build_cards`. They
    # live on the card because the launch band renders per card and the template
    # only ever sees the card, so this is what keeps the configured vocabulary
    # out of the template as a literal.
    launch_models: list["ModelChoice"] = field(default_factory=list)
    launch_efforts: list[str] = field(default_factory=list)
