"""SQLite persistence. The server process is the sole writer.

Migrations are additive only: append to SCHEMA, never rebuild a table.
`upsert_session` stores `ended_at` twice — once as the raw ISO string for
display, once as an epoch int so range queries stay index-friendly.
"""

import contextlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from bridge.models import Handoff, Launch, SessionRecord

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
    CREATE TABLE IF NOT EXISTS project_aliases (
        alias_path TEXT PRIMARY KEY,
        canonical_path TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS handoffs (
        id TEXT PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        source_session_id TEXT,
        summary TEXT,
        next_prompt TEXT NOT NULL,
        suggested_model TEXT,
        suggested_effort TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        created_at INTEGER NOT NULL,
        consumed_at INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_handoffs_project "
    "ON handoffs(project_id, status, created_at)",
    # `mode` is one of terminal | background and `outcome` one of
    # pending | started | failed. Neither is a CHECK constraint, matching
    # `handoffs.status` and `projects.status`: the writing code is the authority
    # on the vocabulary, and a CHECK would make adding a value a table rebuild.
    #
    # `session_id` is nullable and this is forced, not stylistic. `claude --bg`
    # ignores `--session-id` and mints its own, so a background launch has no
    # session id at the moment its row is written; `short_id` holds the 8-hex
    # handle `--bg` prints, which is exactly `session_id[:8]`, and
    # `set_launch_session` fills both in once they are known. Do not tighten
    # this to NOT NULL: that forces a placeholder, and a placeholder in a
    # correlation key is how a launch joins to the wrong session.
    """
    CREATE TABLE IF NOT EXISTS launches (
        id TEXT PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        handoff_id TEXT REFERENCES handoffs(id),
        session_id TEXT,
        short_id TEXT,
        mode TEXT NOT NULL,
        model TEXT,
        effort TEXT,
        prompt TEXT NOT NULL,
        launched_at INTEGER NOT NULL,
        outcome TEXT NOT NULL DEFAULT 'pending'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_launches_project "
    "ON launches(project_id, launched_at)",
    "CREATE INDEX IF NOT EXISTS idx_launches_session ON launches(session_id)",
    """
    CREATE TABLE IF NOT EXISTS git_cache (
        project_id INTEGER PRIMARY KEY REFERENCES projects(id),
        payload_json TEXT NOT NULL,
        probed_at INTEGER NOT NULL
    )
    """,
]

# Additive column migrations. SQLite has no `ADD COLUMN IF NOT EXISTS`, so we
# consult `table_info` and add only what is missing, making every open
# idempotent. Append to this map to evolve a table; never rewrite one.
# (Interpolation is only over our own hardcoded identifiers, never user input.)
COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {}


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
        # `check_same_thread=False` is required because FastAPI dispatches sync
        # routes to a worker threadpool, but it only removes Python's guard rail —
        # it adds no synchronization, and sqlite3's per-connection statement cache
        # is not thread-safe. WAL and busy_timeout govern cross-CONNECTION
        # contention only. This lock is what actually makes the shared connection
        # safe, and it matches the sole-writer architecture.
        self._lock = threading.RLock()
        with self._lock:
            self.conn = sqlite3.connect(
                db_path, timeout=5.0, isolation_level=None, check_same_thread=False
            )
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            for stmt in SCHEMA:
                self.conn.execute(stmt)
            self._ensure_columns()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextlib.contextmanager
    def transaction(self):
        """Group writes so an interrupt cannot advance a scan offset without
        also persisting the session it accounts for. RLock makes the nested
        per-method locks re-entrant.
        """
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                yield
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def _ensure_columns(self) -> None:
        with self._lock:
            for table, columns in COLUMN_MIGRATIONS.items():
                existing = {
                    row["name"]
                    for row in self.conn.execute(f"PRAGMA table_info({table})")
                }
                for name, decl in columns.items():
                    if name not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def upsert_project(self, path: str, name: str) -> int:
        with self._lock:
            self.conn.execute(
                "INSERT INTO projects(path, name, created_at) VALUES(?,?,?) "
                "ON CONFLICT(path) DO NOTHING",
                (path, name, now_epoch()),
            )
            return self.conn.execute(
                "SELECT id FROM projects WHERE path=?", (path,)
            ).fetchone()["id"]

    def project_by_path(self, path: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM projects WHERE path=?", (path,)
            ).fetchone()

    def get_project(self, project_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()

    def set_project_status(self, project_id: int, status: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE projects SET status=? WHERE id=?", (status, project_id)
            )

    def projects(self, include_hidden: bool = False) -> list[sqlite3.Row]:
        with self._lock:
            sql = "SELECT * FROM projects"
            if not include_hidden:
                sql += " WHERE status='active'"
            return list(self.conn.execute(sql + " ORDER BY name"))

    def set_alias(self, alias_path: str, canonical_path: str) -> None:
        """Seeding re-runs on every index, so this must update in place."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO project_aliases(alias_path, canonical_path) VALUES(?,?) "
                "ON CONFLICT(alias_path) DO UPDATE SET "
                "canonical_path=excluded.canonical_path",
                (alias_path, canonical_path),
            )

    def alias_map(self) -> dict[str, str]:
        """Read once per index run; attribution then resolves in memory rather
        than round-tripping per record."""
        with self._lock:
            return {
                row["alias_path"]: row["canonical_path"]
                for row in self.conn.execute(
                    "SELECT alias_path, canonical_path FROM project_aliases"
                )
            }

    def upsert_session(self, rec: SessionRecord, project_id: int) -> None:
        with self._lock:
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

    def session_row(self, session_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()

    def latest_session(self, project_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM sessions WHERE project_id=? "
                "ORDER BY ended_epoch DESC NULLS LAST LIMIT 1",
                (project_id,),
            ).fetchone()

    def sessions(self, project_id: int, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM sessions WHERE project_id=? "
                    "ORDER BY ended_epoch DESC NULLS LAST LIMIT ?",
                    (project_id, limit),
                )
            )

    # --- handoffs: the only authored data here, so the only data that a
    # --- dropped database genuinely loses. See spool.py for the journal.

    def create_handoff(self, h: Handoff, project_id: int) -> str:
        """Queue a handoff, superseding any already queued for the project.

        Both statements run in one transaction: a crash between them would
        otherwise leave the project with its old handoff superseded and no new
        one queued, which is strictly worse than either outcome alone.

        `id<>?` is what makes re-ingesting a spool file harmless. Without it a
        re-drain of the currently queued handoff would supersede *itself* and
        leave nothing queued. `ON CONFLICT DO NOTHING` then makes the insert
        idempotent, so a live POST and a spool drain of the same id cannot both
        insert, and a re-drain cannot resurrect one already consumed.
        """
        with self.transaction():
            self.conn.execute(
                "UPDATE handoffs SET status='superseded' "
                "WHERE project_id=? AND status='queued' AND id<>?",
                (project_id, h.id),
            )
            self.conn.execute(
                "INSERT INTO handoffs(id, project_id, source_session_id, summary, "
                "next_prompt, suggested_model, suggested_effort, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (
                    h.id, project_id, h.source_session_id, h.summary, h.next_prompt,
                    h.suggested_model, h.suggested_effort, h.status or "queued",
                    h.created_at or now_epoch(),
                ),
            )
        return h.id

    def queued_handoff(self, project_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM handoffs WHERE project_id=? AND status='queued' "
                "ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()

    def handoffs(self, project_id: int, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM handoffs WHERE project_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (project_id, limit),
                )
            )

    def get_handoff(self, handoff_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM handoffs WHERE id=?", (handoff_id,)
            ).fetchone()

    def set_handoff_status(self, handoff_id: str, status: str) -> None:
        """`consumed_at` is stamped only on the transition that earns it."""
        with self._lock:
            self.conn.execute(
                "UPDATE handoffs SET status=?, "
                "consumed_at=CASE WHEN ?='consumed' THEN ? ELSE consumed_at END "
                "WHERE id=?",
                (status, status, now_epoch(), handoff_id),
            )

    def update_handoff_prompt(self, handoff_id: str, next_prompt: str) -> None:
        """Persist an inline edit. `status` is deliberately left alone.

        Editing a queued prompt must leave it queued, and `create_handoff` cannot
        do this job: its `ON CONFLICT(id) DO NOTHING` is what makes a re-drained
        spool file idempotent, so an upsert of the same id changes nothing.
        """
        with self._lock:
            self.conn.execute(
                "UPDATE handoffs SET next_prompt=? WHERE id=?",
                (next_prompt, handoff_id),
            )

    def handoff_count(self) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) AS n FROM handoffs"
            ).fetchone()["n"]

    # --- launches: what Bridge spawned. The row is written before the spawn, so
    # --- a failed launch is still a fact the panel can show.

    def create_launch(self, l: Launch) -> str:  # noqa: E741 - matches `h: Handoff`
        """Record a launch. No supersede: every attempt is kept, forever."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO launches(id, project_id, handoff_id, session_id, "
                "mode, model, effort, prompt, launched_at, outcome) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    l.id, l.project_id, l.handoff_id, l.session_id, l.mode, l.model,
                    l.effort, l.prompt, l.launched_at or now_epoch(),
                    l.outcome or "pending",
                ),
            )
        return l.id

    def set_launch_outcome(self, launch_id: str, outcome: str) -> None:
        """Takes a bare string, exactly as `set_handoff_status` does."""
        with self._lock:
            self.conn.execute(
                "UPDATE launches SET outcome=? WHERE id=?", (outcome, launch_id)
            )

    def set_launch_session(
        self, launch_id: str, session_id: str | None, short_id: str | None
    ) -> None:
        """Background mode learns both ids only after the spawn has printed them."""
        with self._lock:
            self.conn.execute(
                "UPDATE launches SET session_id=?, short_id=? WHERE id=?",
                (session_id, short_id, launch_id),
            )

    def launches(self, project_id: int, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM launches WHERE project_id=? "
                    "ORDER BY launched_at DESC LIMIT ?",
                    (project_id, limit),
                )
            )

    def launch_by_session(self, session_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM launches WHERE session_id=?", (session_id,)
            ).fetchone()

    def get_scan_state(self, path: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM scan_state WHERE transcript_path=?", (path,)
            ).fetchone()

    def set_scan_state(
        self, path: str, size: int, mtime: float, offset: int, session_id: str | None
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO scan_state(transcript_path, size, mtime, parsed_offset, session_id) "
                "VALUES(?,?,?,?,?) ON CONFLICT(transcript_path) DO UPDATE SET "
                "size=excluded.size, mtime=excluded.mtime, "
                "parsed_offset=excluded.parsed_offset, session_id=excluded.session_id",
                (path, size, mtime, offset, session_id),
            )

    def token_totals(self, project_id: int, since_epoch: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(tokens_in + tokens_out),0) AS t FROM sessions "
                "WHERE project_id=? AND ended_epoch >= ?",
                (project_id, since_epoch),
            ).fetchone()
            return row["t"]
