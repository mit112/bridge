from dataclasses import dataclass, field


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
