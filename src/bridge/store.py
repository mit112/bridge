"""SQLite persistence. The server process is the sole writer.

Migrations are additive only: append to SCHEMA, never rebuild a table.
`upsert_session` stores `ended_at` twice — once as the raw ISO string for
display, once as an epoch int so range queries stay index-friendly.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bridge.models import SessionRecord

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        pinned INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        title TEXT,
        started_at TEXT,
        ended_at TEXT,
        ended_epoch INTEGER,
        model TEXT,
        effort TEXT,
        git_branch TEXT,
        user_msgs INTEGER NOT NULL DEFAULT 0,
        assistant_msgs INTEGER NOT NULL DEFAULT 0,
        last_prompt TEXT,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        tokens_cache_create INTEGER NOT NULL DEFAULT 0,
        tokens_cache_read INTEGER NOT NULL DEFAULT 0,
        sidechain_tokens INTEGER NOT NULL DEFAULT 0,
        interrupted INTEGER NOT NULL DEFAULT 0,
        transcript_path TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, ended_epoch)",
    """
    CREATE TABLE IF NOT EXISTS scan_state (
        transcript_path TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        parsed_offset INTEGER NOT NULL,
        session_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS git_cache (
        project_id INTEGER PRIMARY KEY REFERENCES projects(id),
        payload_json TEXT NOT NULL,
        probed_at INTEGER NOT NULL
    )
    """,
]


def to_epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class Store:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        for stmt in SCHEMA:
            self.conn.execute(stmt)

    def close(self) -> None:
        self.conn.close()

    def upsert_project(self, path: str, name: str) -> int:
        self.conn.execute(
            "INSERT INTO projects(path, name, created_at) VALUES(?,?,?) "
            "ON CONFLICT(path) DO NOTHING",
            (path, name, now_epoch()),
        )
        return self.conn.execute(
            "SELECT id FROM projects WHERE path=?", (path,)
        ).fetchone()["id"]

    def set_project_status(self, project_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE projects SET status=? WHERE id=?", (status, project_id)
        )

    def projects(self, include_hidden: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM projects"
        if not include_hidden:
            sql += " WHERE status='active'"
        return list(self.conn.execute(sql + " ORDER BY name"))

    def upsert_session(self, rec: SessionRecord, project_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions (
                id, project_id, title, started_at, ended_at, ended_epoch, model,
                effort, git_branch, user_msgs, assistant_msgs, last_prompt,
                tokens_in, tokens_out, tokens_cache_create, tokens_cache_read,
                sidechain_tokens, interrupted, transcript_path
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, started_at=excluded.started_at,
                ended_at=excluded.ended_at, ended_epoch=excluded.ended_epoch,
                model=excluded.model, effort=excluded.effort,
                git_branch=excluded.git_branch, user_msgs=excluded.user_msgs,
                assistant_msgs=excluded.assistant_msgs,
                last_prompt=excluded.last_prompt, tokens_in=excluded.tokens_in,
                tokens_out=excluded.tokens_out,
                tokens_cache_create=excluded.tokens_cache_create,
                tokens_cache_read=excluded.tokens_cache_read,
                sidechain_tokens=excluded.sidechain_tokens,
                interrupted=excluded.interrupted,
                transcript_path=excluded.transcript_path
            """,
            (
                rec.session_id, project_id, rec.title, rec.started_at, rec.ended_at,
                to_epoch(rec.ended_at), rec.model, rec.effort, rec.git_branch,
                rec.user_msgs, rec.assistant_msgs, rec.last_prompt, rec.tokens_in,
                rec.tokens_out, rec.tokens_cache_create, rec.tokens_cache_read,
                rec.sidechain_tokens, int(rec.interrupted), rec.transcript_path,
            ),
        )

    def latest_session(self, project_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE project_id=? "
            "ORDER BY ended_epoch DESC NULLS LAST LIMIT 1",
            (project_id,),
        ).fetchone()

    def sessions(self, project_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM sessions WHERE project_id=? "
                "ORDER BY ended_epoch DESC NULLS LAST LIMIT ?",
                (project_id, limit),
            )
        )

    def get_scan_state(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scan_state WHERE transcript_path=?", (path,)
        ).fetchone()

    def set_scan_state(
        self, path: str, size: int, mtime: float, offset: int, session_id: str | None
    ) -> None:
        self.conn.execute(
            "INSERT INTO scan_state(transcript_path, size, mtime, parsed_offset, session_id) "
            "VALUES(?,?,?,?,?) ON CONFLICT(transcript_path) DO UPDATE SET "
            "size=excluded.size, mtime=excluded.mtime, "
            "parsed_offset=excluded.parsed_offset, session_id=excluded.session_id",
            (path, size, mtime, offset, session_id),
        )

    def token_totals(self, project_id: int, since_epoch: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(tokens_in + tokens_out),0) AS t FROM sessions "
            "WHERE project_id=? AND ended_epoch >= ?",
            (project_id, since_epoch),
        ).fetchone()
        return row["t"]
