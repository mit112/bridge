"""SQLite persistence. The server process is the sole writer.

Migrations are additive only: append to SCHEMA, never rebuild a table.
`upsert_session` stores `ended_at` twice — once as the raw ISO string for
display, once as an epoch int so range queries stay index-friendly.
"""

import contextlib
import dataclasses
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from bridge.models import GitState, Handoff, Launch, ScheduledRun, SessionRecord

logger = logging.getLogger(__name__)

# Whitelisted sort vocabularies for the paged history tables. Keys are the
# public `?sort=` values (safe to echo into a URL and template); values are
# trusted, hardcoded SQL column expressions -- NEVER a caller-supplied name --
# so the ORDER BY can be parameterized without opening an injection. The first
# entry in each map is that table's default sort; an unknown key falls back to
# it, so a hand-typed or hostile `?sort=` can only ever pick a whitelisted
# expression. Sorting has to happen here (server-side): the tables are paged,
# and a client reorder of the visible slice would misorder every row past the
# cap.
SESSION_SORTS = {
    "ended": "ended_epoch",
    "title": "title",
    "model": "model",
    "turns": "user_msgs + assistant_msgs",
    "tokens": "tokens_in + tokens_out",
}
HANDOFF_SORTS = {"created": "created_at", "status": "status"}
LAUNCH_SORTS = {
    "launched": "launched_at",
    "mode": "mode",
    "model": "model",
    "outcome": "outcome",
}


def _order_by(whitelist: dict[str, str], sort: str | None, direction: str | None) -> str:
    """Build a trusted `ORDER BY` clause from a per-table whitelist.

    `sort` is looked up in the whitelist; an unknown/absent key falls back to
    the whitelist's first (default) column, so nothing a caller supplies is
    ever interpolated raw. `direction` normalizes to DESC unless it is exactly
    "asc". `NULLS LAST` keeps blank cells at the bottom in either direction --
    the same treatment the pre-sort `ended_epoch DESC NULLS LAST` default gave.
    """
    default_col = next(iter(whitelist.values()))
    col = whitelist.get(sort or "", default_col)
    dir_sql = "ASC" if direction == "asc" else "DESC"
    return f"ORDER BY {col} {dir_sql} NULLS LAST"


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
    CREATE TABLE IF NOT EXISTS index_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ran_at INTEGER NOT NULL,
        files_seen INTEGER,
        files_scanned INTEGER,
        lines_parsed INTEGER,
        parse_errors INTEGER,
        sessions_upserted INTEGER,
        duration_ms INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS git_cache (
        project_id INTEGER PRIMARY KEY REFERENCES projects(id),
        payload_json TEXT NOT NULL,
        probed_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_runs (
        id TEXT PRIMARY KEY,
        project_path TEXT NOT NULL,
        prompt TEXT NOT NULL,
        summary TEXT,
        model TEXT,
        effort TEXT,
        mode TEXT NOT NULL,
        permission_mode TEXT,
        source_handoff_id TEXT,
        scheduled_for INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL,
        claimed_at INTEGER,
        completed_at INTEGER,
        fired_at INTEGER,
        launch_id TEXT,
        error TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scheduled_runs_status "
    "ON scheduled_runs(status, scheduled_for)",
]

# Additive column migrations. SQLite has no `ADD COLUMN IF NOT EXISTS`, so we
# consult `table_info` and add only what is missing, making every open
# idempotent. Append to this map to evolve a table; never rewrite one.
# (Interpolation is only over our own hardcoded identifiers, never user input.)
COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    # Carries the transcript scanner's usage-dedup state across index runs. An
    # incremental scan can resume in the middle of one API response's entries,
    # and without this the totals for actively-running sessions triple again.
    "sessions": {"last_usage_request_id": "TEXT"},
    # Epoch of the first reindex that found this project's path gone, so a later
    # manual restore in the panel is never re-archived. NULL = never seen missing.
    "projects": {"missing_archived_at": "INTEGER"},
    # The id of the failed/indeterminate run this row was created to retry.
    # NULL for every schedule a person authored. Deliberately not a foreign key:
    # `prune_scheduled_runs` reaps an old original before its newer retry, and a
    # dangling provenance pointer is a better outcome than a delete that fails.
    "scheduled_runs": {"retry_of": "TEXT"},
}


# How long a finished scheduled run stays in the table. Long enough that a
# schedule you set up a month ago and forgot is still explicable, short enough
# that the table does not grow without bound under the panel's manual-`serve`
# uptime model, where startup is the only housekeeping event there is.
SCHEDULED_RUN_RETENTION_DAYS = 30


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

    def archive_missing(self, project_id: int, at: int) -> None:
        """Archive a project whose directory has vanished, stamping WHEN we acted.

        The stamp is the whole point: the auto-archive pass skips any project that
        already carries one, so a user who restores a still-missing project in the
        panel is not silently re-archived on the next index. This is the config
        seed-vs-override rule applied one row over.
        """
        with self._lock:
            self.conn.execute(
                "UPDATE projects SET status='archived', missing_archived_at=? WHERE id=?",
                (at, project_id),
            )

    def set_project_pinned(self, project_id: int, pinned: bool) -> None:
        """Stored as 0/1 because the column is INTEGER and SQLite has no bool.

        Separate from `set_project_status`: pinning and hiding are independent
        decisions, and a caller changing one must not have to restate the other.
        """
        with self._lock:
            self.conn.execute(
                "UPDATE projects SET pinned=? WHERE id=?", (int(pinned), project_id)
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
                    sidechain_tokens, interrupted, transcript_path,
                    last_usage_request_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    transcript_path=excluded.transcript_path,
                    last_usage_request_id=excluded.last_usage_request_id
                """,
                (
                    rec.session_id, project_id, rec.title, rec.started_at, rec.ended_at,
                    to_epoch(rec.ended_at), rec.model, rec.effort, rec.git_branch,
                    rec.user_msgs, rec.assistant_msgs, rec.last_prompt, rec.tokens_in,
                    rec.tokens_out, rec.tokens_cache_create, rec.tokens_cache_read,
                    rec.sidechain_tokens, int(rec.interrupted), rec.transcript_path,
                    rec.last_usage_request_id,
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

    def sessions(
        self, project_id: int, limit: int = 50, offset: int = 0,
        *, sort: str | None = None, direction: str | None = None,
        model: str | None = None,
    ) -> list[sqlite3.Row]:
        order = _order_by(SESSION_SORTS, sort, direction)
        where = "WHERE project_id=?"
        params: list = [project_id]
        if model is not None:
            where += " AND model=?"
            params.append(model)
        params += [limit, offset]
        with self._lock:
            return list(
                self.conn.execute(
                    f"SELECT * FROM sessions {where} {order} LIMIT ? OFFSET ?",
                    params,
                )
            )

    def count_sessions(self, project_id: int, *, model: str | None = None) -> int:
        """The total behind a paged `sessions()` call, for the capped history.

        `model`, when given, counts only the filtered slice so the pager states
        the true total for the active filter, exactly as `sessions()` returns it.
        """
        where = "WHERE project_id=?"
        params: list = [project_id]
        if model is not None:
            where += " AND model=?"
            params.append(model)
        with self._lock:
            return self.conn.execute(
                f"SELECT COUNT(*) AS n FROM sessions {where}", params
            ).fetchone()["n"]

    def session_model_facets(self, project_id: int) -> list[tuple[str, int]]:
        """(model, count) over the UNFILTERED session set, so the filter menu
        and its counts never shift as the user narrows down. A null model is not
        an offerable facet -- only the "All" choice ever includes those rows."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT model, COUNT(*) AS n FROM sessions "
                "WHERE project_id=? AND model IS NOT NULL "
                "GROUP BY model ORDER BY model",
                (project_id,),
            )
            return [(r["model"], r["n"]) for r in rows]

    # --- handoffs: authored data, not derived from any transcript, so a
    # --- dropped database loses them. See spool.py for the journal.
    # --- `scheduled_runs` is the other authored table; see schedspool.py.

    def create_handoff(self, h: Handoff, project_id: int) -> str:
        """Queue a handoff, superseding any already queued for the SAME session.

        Supersession is scoped to `source_session_id` so two different sessions on
        one project each keep their queued handoff, while re-running the skill in the
        same session still replaces that session's own prompt. A null session never
        supersedes: anonymous handoffs have no identity to collapse against, so each
        stands alone. The `id<>?` guard and `ON CONFLICT(id) DO NOTHING` keep a spool
        re-drain idempotent exactly as before.

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
            if h.source_session_id is not None:
                self.conn.execute(
                    "UPDATE handoffs SET status='superseded' "
                    "WHERE project_id=? AND status='queued' "
                    "AND source_session_id=? AND id<>?",
                    (project_id, h.source_session_id, h.id),
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

    def queued_handoffs(self, project_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(
                "SELECT * FROM handoffs WHERE project_id=? AND status='queued' "
                "ORDER BY created_at DESC",
                (project_id,),
            ))

    def queued_handoff(self, project_id: int) -> sqlite3.Row | None:
        """The newest queued handoff, or None. Retained for callers that still want
        a single 'what's next'; `queued_handoffs` is the full set."""
        rows = self.queued_handoffs(project_id)
        return rows[0] if rows else None

    def handoffs(
        self, project_id: int, limit: int = 50, offset: int = 0,
        *, sort: str | None = None, direction: str | None = None,
        status: str | None = None,
    ) -> list[sqlite3.Row]:
        order = _order_by(HANDOFF_SORTS, sort, direction)
        where = "WHERE project_id=?"
        params: list = [project_id]
        if status is not None:
            where += " AND status=?"
            params.append(status)
        params += [limit, offset]
        with self._lock:
            return list(
                self.conn.execute(
                    f"SELECT * FROM handoffs {where} {order} LIMIT ? OFFSET ?",
                    params,
                )
            )

    def count_handoffs(self, project_id: int, *, status: str | None = None) -> int:
        """The total behind a paged `handoffs()` call. Distinct from
        `handoff_count`, which counts every handoff in the store, not one
        project's history. `status` narrows the count to the active filter."""
        where = "WHERE project_id=?"
        params: list = [project_id]
        if status is not None:
            where += " AND status=?"
            params.append(status)
        with self._lock:
            return self.conn.execute(
                f"SELECT COUNT(*) AS n FROM handoffs {where}", params
            ).fetchone()["n"]

    def handoff_status_facets(self, project_id: int) -> list[tuple[str, int]]:
        """(status, count) over the UNFILTERED handoff set -- the filter menu
        and its counts stay stable no matter which status is active."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM handoffs "
                "WHERE project_id=? GROUP BY status ORDER BY status",
                (project_id,),
            )
            return [(r["status"], r["n"]) for r in rows]

    def get_handoff(self, handoff_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM handoffs WHERE id=?", (handoff_id,)
            ).fetchone()

    def claim_queued_handoff(self, handoff_id: str, project_id: int) -> sqlite3.Row | None:
        """Atomically move a queued handoff to `launching`, scoped to the
        project that owns it. Returns the pre-claim row (still carrying
        `next_prompt`/`summary`) on success, or `None` if the id does not
        exist, belongs to a DIFFERENT project, or is no longer `queued` --
        the three ways a stale or foreign `handoff_id` could otherwise still
        fire a session. `WHERE ... AND status='queued'` on the UPDATE itself
        (not just the preceding SELECT) is what makes this safe under two
        concurrent launches of the same handoff: only one can move the row,
        and the other's rowcount comes back 0.

        `launching`, not `consumed`, so a spawn that then fails can be
        reverted with `revert_claimed_handoff` -- the handoff must stay
        available for a retry, exactly as it did before claiming existed.
        """
        with self.transaction():
            row = self.conn.execute(
                "SELECT * FROM handoffs WHERE id=? AND project_id=? AND status='queued'",
                (handoff_id, project_id),
            ).fetchone()
            if row is None:
                return None
            cur = self.conn.execute(
                "UPDATE handoffs SET status='launching' "
                "WHERE id=? AND project_id=? AND status='queued'",
                (handoff_id, project_id),
            )
            if cur.rowcount != 1:
                return None
            return row

    def revert_claimed_handoff(self, handoff_id: str) -> None:
        """Undo `claim_queued_handoff` when the spawn itself fails. Scoped to
        `status='launching'` so this can never resurrect a handoff some other
        transition (dismiss, a since-completed launch) has already moved on
        from."""
        with self._lock:
            self.conn.execute(
                "UPDATE handoffs SET status='queued' WHERE id=? AND status='launching'",
                (handoff_id,),
            )

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

    def queued_handoff_count(self) -> int:
        """Only the ones still waiting. `handoff_count` counts history too, and
        diagnostics reporting every handoff ever written as a backlog would be
        the same mistake as counting drained spool files as depth."""
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) AS n FROM handoffs WHERE status='queued'"
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

    def pending_launch_ids(self) -> list[str]:
        """Name the strays `reconcile_pending_launches` would flip, without
        flipping. `pending` is the row's state from the moment it is
        inserted until a terminal outcome is recorded (`_started`/`_failed`
        in launcher.py) -- a process killed in between (or one whose
        `set_launch_outcome` call itself raised) leaves it here forever,
        indistinguishable from a launch still genuinely in flight one
        second ago. Only a fresh boot can tell the difference: nothing THIS
        process just started can still be pending by the time it asks."""
        with self._lock:
            return [
                r["id"] for r in
                self.conn.execute("SELECT id FROM launches WHERE outcome='pending'")
            ]

    def reconcile_pending_launches(self, ids: list[str]) -> int:
        """Flip a stray `pending` launch to `indeterminate` -- a real spawn
        may or may not have happened, so this is terminal and never retried
        automatically, exactly as `reconcile_launching` treats a stray
        scheduled run. Any handoff those launches had claimed (moved to
        `launching` by `claim_queued_handoff`, never resolved to `consumed`
        or reverted to `queued` because the process died first) moves to
        `indeterminate` alongside it, so it stops being invisible -- neither
        offered as queued again nor silently stuck in a status nothing
        displays.
        """
        if not ids:
            return 0
        with self.transaction():
            placeholders = ",".join("?" for _ in ids)
            handoff_ids = [
                r["handoff_id"] for r in self.conn.execute(
                    f"SELECT handoff_id FROM launches WHERE id IN ({placeholders})",
                    ids,
                )
                if r["handoff_id"] is not None
            ]
            cur = self.conn.execute(
                f"UPDATE launches SET outcome='indeterminate' "
                f"WHERE id IN ({placeholders}) AND outcome='pending'",
                ids,
            )
            if handoff_ids:
                hph = ",".join("?" for _ in handoff_ids)
                self.conn.execute(
                    f"UPDATE handoffs SET status='indeterminate' "
                    f"WHERE id IN ({hph}) AND status='launching'",
                    handoff_ids,
                )
            return cur.rowcount

    def launches(
        self, project_id: int, limit: int = 50, offset: int = 0,
        *, sort: str | None = None, direction: str | None = None,
        outcome: str | None = None,
    ) -> list[sqlite3.Row]:
        order = _order_by(LAUNCH_SORTS, sort, direction)
        where = "WHERE project_id=?"
        params: list = [project_id]
        if outcome is not None:
            where += " AND outcome=?"
            params.append(outcome)
        params += [limit, offset]
        with self._lock:
            return list(
                self.conn.execute(
                    f"SELECT * FROM launches {where} {order} LIMIT ? OFFSET ?",
                    params,
                )
            )

    def count_launches(self, project_id: int, *, outcome: str | None = None) -> int:
        """The total behind a paged `launches()` call, for the capped history.
        `outcome` narrows the count to the active filter."""
        where = "WHERE project_id=?"
        params: list = [project_id]
        if outcome is not None:
            where += " AND outcome=?"
            params.append(outcome)
        with self._lock:
            return self.conn.execute(
                f"SELECT COUNT(*) AS n FROM launches {where}", params
            ).fetchone()["n"]

    def launch_outcome_facets(self, project_id: int) -> list[tuple[str, int]]:
        """(outcome, count) over the UNFILTERED launch set, so the filter menu
        and its counts stay stable no matter which outcome is active."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT outcome, COUNT(*) AS n FROM launches "
                "WHERE project_id=? GROUP BY outcome ORDER BY outcome",
                (project_id,),
            )
            return [(r["outcome"], r["n"]) for r in rows]

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

    # --- index_runs ---------------------------------------------------------
    #
    # `record_index_run` takes the stats dict the indexer already returns, so
    # the indexer's return shape stays the contract and nothing new is invented
    # for diagnostics to read.

    _RUN_FIELDS = ("files_seen", "files_scanned", "lines_parsed",
                   "parse_errors", "sessions_upserted")

    def record_index_run(self, stats: dict, ran_at: int, duration_ms: int) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO index_runs (ran_at, files_seen, files_scanned, "
                "lines_parsed, parse_errors, sessions_upserted, duration_ms) "
                "VALUES (?,?,?,?,?,?,?)",
                (ran_at, *(int(stats.get(f) or 0) for f in self._RUN_FIELDS),
                 duration_ms),
            )

    def latest_index_run(self):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM index_runs ORDER BY ran_at DESC, id DESC LIMIT 1"
            ).fetchone()

    # --- git_cache ----------------------------------------------------------
    #
    # The table has existed since Phase 1 and nothing ever read or wrote it.
    # Only `status == "ok"` is written and only `status == "unavailable"` reads
    # it back: `unavailable` is the one genuinely transient outcome (timeout,
    # disk asleep), while `not_a_repo` is stable truth and must be allowed to
    # say so rather than showing a fossil.

    def put_git_cache(self, project_id: int, git: GitState, probed_at: int) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO git_cache (project_id, payload_json, probed_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "payload_json=excluded.payload_json, probed_at=excluded.probed_at",
                (project_id, json.dumps(dataclasses.asdict(git)), probed_at),
            )

    def get_git_cache(self, project_id: int) -> tuple[GitState, int] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT payload_json, probed_at FROM git_cache WHERE project_id=?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        # Filtered to fields `GitState` actually has. The cache is a JSON blob
        # that outlives any one version of the dataclass, so a field added or
        # removed between writes must degrade rather than raise -- a bare
        # `GitState(**payload)` would turn every cache hit into a TypeError and
        # take the whole dashboard down.
        known = {f.name for f in dataclasses.fields(GitState)}
        return (
            GitState(**{k: v for k, v in payload.items() if k in known}),
            row["probed_at"],
        )

    def token_series(self, project_id: int, days: int, now: int) -> list[int]:
        """Daily token totals, oldest first, exactly `days` long.

        Gaps are filled with zeros: a sparkline whose x-axis skips idle days
        compresses time and misrepresents the shape.

        Sums `tokens_in + tokens_out` and NOT the cache columns, matching
        `token_totals` exactly. The sparkline renders inches from that method's
        "23k today", so a broader definition here would draw a line whose
        magnitude visibly disagrees with the number beside it. If the definition
        of burn ever changes, both must change together.
        """
        start = now - days * 86400
        with self._lock:
            rows = self.conn.execute(
                "SELECT (ended_epoch - ?) / 86400 AS bucket, "
                "COALESCE(SUM(tokens_in + tokens_out),0) AS total "
                "FROM sessions WHERE project_id=? AND ended_epoch >= ? "
                "GROUP BY bucket",
                (start, project_id, start),
            ).fetchall()
        series = [0] * days
        for row in rows:
            # The WHERE clause is the only thing keeping `bucket` non-negative,
            # and a negative index would silently write from the END of the
            # list rather than raise. The upper bound is still checked here
            # because `now` can sit mid-day, putting today's rows in bucket
            # `days` exactly.
            bucket = int(row["bucket"])
            if bucket < days:
                series[bucket] = int(row["total"] or 0)
        return series

    def token_totals(self, project_id: int, since_epoch: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(tokens_in + tokens_out),0) AS t FROM sessions "
                "WHERE project_id=? AND ended_epoch >= ?",
                (project_id, since_epoch),
            ).fetchone()
            return row["t"]

    # --- scheduled_runs -------------------------------------------------------
    #
    # Every state transition below is one conditional `UPDATE ... WHERE
    # status='...'` inside a transaction, checked via rowcount (or re-read for
    # the caller). This is what makes a repeat claim, a stray edit against an
    # already-launching job, or a double-fire of the scheduler all lose safely
    # rather than corrupt the row.

    def create_scheduled_run(self, job: ScheduledRun) -> str:
        with self._lock:
            self.conn.execute(
                "INSERT INTO scheduled_runs (id, project_path, prompt, summary, "
                "model, effort, mode, permission_mode, source_handoff_id, "
                "scheduled_for, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.id, job.project_path, job.prompt, job.summary, job.model,
                    job.effort, job.mode, job.permission_mode, job.source_handoff_id,
                    job.scheduled_for, job.status, job.created_at or now_epoch(),
                ),
            )
        return job.id

    def restore_scheduled_run(self, job: ScheduledRun) -> str:
        """Insert a scheduled run with its terminal columns intact.

        Journal replay only. `create_scheduled_run` deliberately omits
        `completed_at`, `fired_at`, `launch_id` and `error` because a newly
        authored schedule has none of them; a recovered one can have all four,
        and a terminal row that arrives with `completed_at` NULL is invisible to
        retention forever. Kept separate rather than widening
        `create_scheduled_run` so that only recovery can write a terminal row
        directly.
        """
        with self._lock:
            self.conn.execute(
                "INSERT INTO scheduled_runs (id, project_path, prompt, summary, "
                "model, effort, mode, permission_mode, source_handoff_id, "
                "scheduled_for, status, created_at, claimed_at, completed_at, "
                "fired_at, launch_id, error, retry_of) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.id, job.project_path, job.prompt, job.summary, job.model,
                    job.effort, job.mode, job.permission_mode, job.source_handoff_id,
                    job.scheduled_for, job.status, job.created_at or now_epoch(),
                    job.claimed_at, job.completed_at, job.fired_at, job.launch_id,
                    job.error, job.retry_of,
                ),
            )
        return job.id

    def scheduled_runs(
        self, status: str | None = None, limit: int | None = None, offset: int = 0
    ) -> list[sqlite3.Row]:
        with self._lock:
            sql = "SELECT * FROM scheduled_runs"
            params: tuple = ()
            if status is not None:
                sql += " WHERE status=?"
                params = (status,)
            sql += (
                " ORDER BY CASE WHEN status IN ('pending','launching') THEN 0 "
                "ELSE 1 END, scheduled_for"
            )
            if limit is not None or offset:
                # SQLite has no bare OFFSET: a negative LIMIT is its documented
                # way to say "all of them, starting from here".
                sql += " LIMIT ? OFFSET ?"
                params = (*params, -1 if limit is None else limit, offset)
            return list(self.conn.execute(sql, params))

    def count_scheduled_runs(self, status: str | None = None) -> int:
        """The total behind a paged `scheduled_runs()` call."""
        with self._lock:
            sql = "SELECT COUNT(*) AS n FROM scheduled_runs"
            params: tuple = ()
            if status is not None:
                sql += " WHERE status=?"
                params = (status,)
            return self.conn.execute(sql, params).fetchone()["n"]

    def get_scheduled_run(self, id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM scheduled_runs WHERE id=?", (id,)
            ).fetchone()

    def claim_one_due(self, now: int) -> sqlite3.Row | None:
        with self.transaction():
            row = self.conn.execute(
                "SELECT * FROM scheduled_runs WHERE status='pending' AND scheduled_for<=? "
                "ORDER BY scheduled_for, created_at, id LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            cur = self.conn.execute(
                "UPDATE scheduled_runs SET status='launching', claimed_at=? "
                "WHERE id=? AND status='pending'", (now, row["id"]))
            if cur.rowcount != 1:
                return None                      # lost the race
            return self.get_scheduled_run(row["id"])

    def claim_specific(self, id: str) -> sqlite3.Row | None:
        with self.transaction():
            cur = self.conn.execute(
                "UPDATE scheduled_runs SET status='launching', claimed_at=? "
                "WHERE id=? AND status='pending'", (now_epoch(), id))
            return self.get_scheduled_run(id) if cur.rowcount == 1 else None

    def finish_scheduled_run(
        self, id: str, *, status: str, launch_id: str | None = None,
        error: str | None = None, fired_at: int | None = None,
    ) -> None:
        with self.transaction():
            cur = self.conn.execute(
                "UPDATE scheduled_runs SET status=?, launch_id=?, error=?, fired_at=?, "
                "completed_at=? WHERE id=? AND status='launching'",
                (status, launch_id, error, fired_at, now_epoch(), id))
            if cur.rowcount != 1:
                # Double-finish, or a finish that lost a race with
                # reconcile_launching flipping the row to 'indeterminate'
                # first. The write is silently lost by design (no other
                # status should be clobbered) but that loss must be visible.
                logger.warning(
                    "finish_scheduled_run(%r, status=%r): no row in "
                    "'launching' state; write discarded", id, status)

    def edit_pending(self, id: str, **fields) -> bool:
        # `fields` keys are always hardcoded caller kwargs (never user input
        # used as a key), matching the COLUMN_MIGRATIONS interpolation note.
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.transaction():
            cur = self.conn.execute(
                f"UPDATE scheduled_runs SET {cols} WHERE id=? AND status='pending'",
                (*fields.values(), id))
            return cur.rowcount == 1

    def cancel_pending(self, id: str) -> bool:
        with self.transaction():
            cur = self.conn.execute(
                "UPDATE scheduled_runs SET status='cancelled', completed_at=? "
                "WHERE id=? AND status='pending'", (now_epoch(), id))
            return cur.rowcount == 1

    def retry_terminal(
        self, id: str, *, new_id: str, now: int | None = None
    ) -> sqlite3.Row | None:
        """Copy a `failed`/`indeterminate` run into a fresh, already-claimed row.

        A retry is a NEW row rather than a terminal row walked backwards: the
        failure is the only record of what went wrong, and the retry needs its
        own claim, launch id and outcome anyway. Crucially it carries
        `source_handoff_id` across, which is the whole reason this exists --
        the panel's old retry POSTed `/api/launch` with no handoff, so retrying
        a schedule born from a handoff left that handoff queued forever.

        One `INSERT ... SELECT`, in the same conditional-transition spirit as
        every method above: the guard is in the WHERE clause and the rowcount
        is the answer, so a second retry (two tabs, a double click) loses
        safely instead of producing a second launch. Retries chain -- retry the
        retry -- which is also what the panel offers, since the row a user
        watches fail is always the newest one.

        Returns the new row already in `launching`, ready for
        `_fire_claimed_job`; `None` if `id` is unknown, is not in a retryable
        state, or has been retried already.

        `missed` is retryable for the same reason `failed` is: the run never
        launched. It reaches this method only from journal replay, and without
        it a recovered schedule could not be run at all -- `run-now` requires
        `pending`.
        """
        when = now if now is not None else now_epoch()
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO scheduled_runs ("
                "  id, project_path, prompt, summary, model, effort, mode, "
                "  permission_mode, source_handoff_id, scheduled_for, status, "
                "  created_at, claimed_at, retry_of) "
                "SELECT ?, orig.project_path, orig.prompt, orig.summary, "
                "  orig.model, orig.effort, orig.mode, orig.permission_mode, "
                "  orig.source_handoff_id, ?, 'launching', ?, ?, orig.id "
                "FROM scheduled_runs AS orig "
                "WHERE orig.id=? AND orig.status IN ('failed','indeterminate','missed') "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM scheduled_runs r WHERE r.retry_of = orig.id)",
                (new_id, when, when, when, id))
            return self.get_scheduled_run(new_id) if cur.rowcount == 1 else None

    def prunable_scheduled_run_ids(self, before_epoch: int) -> list[str]:
        """Name the rows `prune_scheduled_runs` would delete, without deleting.

        The status and age clauses are the ones that used to live in the DELETE;
        see `prune_scheduled_runs` for why the status clause is unfalsifiable.
        """
        with self._lock:
            return [
                r["id"] for r in self.conn.execute(
                    "SELECT id FROM scheduled_runs "
                    "WHERE status NOT IN ('pending','launching') "
                    "  AND completed_at < ?", (before_epoch,))
            ]

    def prune_scheduled_runs(self, ids: list[str]) -> int:
        """Delete exactly the runs named, returning how many went.

        The age and status bounds now live in `prunable_scheduled_run_ids`;
        this method trusts its argument. Only rows that are done should ever
        reach it: a `pending` job is still owed a launch no matter how long ago
        it was authored, and a `launching` one is either in flight or waiting
        for the next boot's `reconcile_launching`.

        The status clause is deliberately unfalsifiable, and there is no
        mutation for it. `completed_at` is written by exactly one place --
        finishing, cancelling or reconciling a row, all of which are terminal
        -- so over every reachable state a non-terminal row carries NULL, and
        `completed_at < ?` is NULL rather than true for it (SQL three-valued
        logic) and excludes it anyway. Either clause alone is therefore
        sufficient, so no test can tell them apart. This one stays because it
        states the intent the age bound only implies.

        Split from the id query above it so the caller can journal a `pruned`
        record per row *before* the row disappears. Journaling afterwards would
        mean a swallowed write leaves a deleted row with no marker, and replay
        would resurrect it.
        """
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self.transaction():
            cur = self.conn.execute(
                f"DELETE FROM scheduled_runs WHERE id IN ({marks})", tuple(ids))
            return cur.rowcount

    def launching_scheduled_run_ids(self) -> list[str]:
        """Name the strays `reconcile_launching` would flip, without flipping."""
        with self._lock:
            return [
                r["id"] for r in self.conn.execute(
                    "SELECT id FROM scheduled_runs WHERE status='launching'")
            ]

    def reconcile_launching(self, now: int, ids: list[str]) -> int:
        """Flip the named stray `launching` rows to `indeterminate`.

        Takes explicit ids rather than flipping every `launching` row, so the
        caller can journal first and skip any row whose record would not write.
        Leaving an unjournalled row `launching` is the safe failure: the next
        boot reconciles it again, whereas flipping it without a record lets a
        run-now'd future job replay as `pending` and fire twice.
        """
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self.transaction():
            cur = self.conn.execute(
                f"UPDATE scheduled_runs SET status='indeterminate', completed_at=? "
                f"WHERE status='launching' AND id IN ({marks})", (now, *ids))
            return cur.rowcount
