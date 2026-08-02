"""Opportunistic read of /insights session-meta files.

`~/.claude/usage-data/session-meta/{session_id}.json` is the `/insights`
output, capped at 200 sessions newest-first. It is enrichment only: a missing,
malformed, or mismatched file is the ordinary case for any session older than
the newest 200, and is never an error.

The token fields these files carry (`input_tokens`/`output_tokens`) are
DELIBERATELY not read. The transcript parse is Bridge's sole token authority;
a second, disagreeing number is the exact failure this module must not create.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_META_DIR = Path.home() / ".claude" / "usage-data" / "session-meta"


@dataclass(frozen=True)
class SessionMeta:
    files_modified: int
    lines_added: int
    lines_removed: int
    git_commits: int
    git_pushes: int
    duration_minutes: int
    tool_errors: int
    user_interruptions: int
    uses_task_agent: bool
    uses_mcp: bool
    uses_web: bool

    @property
    def has_signal(self) -> bool:
        return bool(
            self.files_modified or self.lines_added or self.lines_removed
            or self.git_commits or self.git_pushes or self.duration_minutes
            or self.tool_errors or self.user_interruptions
            or self.uses_task_agent or self.uses_mcp or self.uses_web
        )


def _int(raw: dict, key: str) -> int:
    try:
        return int(raw.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def read(session_id: str, meta_dir: Path = DEFAULT_META_DIR) -> SessionMeta | None:
    try:
        raw = json.loads(
            (Path(meta_dir) / f"{session_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("session_id") != session_id:
        return None
    return SessionMeta(
        files_modified=_int(raw, "files_modified"),
        lines_added=_int(raw, "lines_added"),
        lines_removed=_int(raw, "lines_removed"),
        git_commits=_int(raw, "git_commits"),
        git_pushes=_int(raw, "git_pushes"),
        duration_minutes=_int(raw, "duration_minutes"),
        tool_errors=_int(raw, "tool_errors"),
        user_interruptions=_int(raw, "user_interruptions"),
        uses_task_agent=bool(raw.get("uses_task_agent")),
        uses_mcp=bool(raw.get("uses_mcp")),
        uses_web=bool(raw.get("uses_web_search")) or bool(raw.get("uses_web_fetch")),
    )


def read_many(session_ids, meta_dir: Path = DEFAULT_META_DIR) -> dict[str, SessionMeta]:
    out: dict[str, SessionMeta] = {}
    for sid in session_ids:
        m = read(sid, meta_dir)
        if m is not None and m.has_signal:
            out[sid] = m
    return out
