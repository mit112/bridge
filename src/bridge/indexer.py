"""Walk transcripts into the store, reading only what changed.

A file whose recorded size and mtime both match is never reopened. A file that
shrank was rewritten and is re-scanned from offset zero. One bad file never
aborts a run.
"""

import dataclasses
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bridge import backfill
from bridge.config import Config
from bridge.models import SessionRecord
from bridge.registry import display_name, transcript_files
from bridge.store import Store, now_epoch
from bridge.transcripts import scan


@dataclass
class IndexStats:
    files_seen: int = 0
    files_scanned: int = 0
    lines_parsed: int = 0
    parse_errors: int = 0
    sessions_upserted: int = 0
    launches_linked: int = 0


def reindex(
    store: Store, cfg: Config, progress: Callable[[int, int], None] | None = None
) -> IndexStats:
    started = time.monotonic()
    stats = IndexStats()
    files = transcript_files(cfg.claude_projects_dir)
    total = len(files)

    # Seed before indexing so this run's sessions attribute to canonical paths,
    # and read back the union of config-declared and already-stored aliases.
    for alias, canonical in cfg.aliases.items():
        store.set_alias(alias, canonical)
    aliases = store.alias_map()

    # Config seeds; the database overrides. Which archived paths are new has to
    # be settled BEFORE indexing, because indexing is what creates their rows.
    unseen_archived = [
        p for p in cfg.archived_paths if store.project_by_path(p) is None
    ]

    for i, path in enumerate(files):
        stats.files_seen += 1
        if progress:
            progress(i + 1, total)
        try:
            _index_one(store, path, stats, aliases)
        except OSError:
            continue  # file vanished or unreadable mid-run; never fatal

    # Discover git repos under every discovery path that have no transcripts
    # yet, so a repo you have not opened in Claude still gets a card
    # (spec:240-241). Opt-out: hide it if you don't want it. `upsert_project` is ON CONFLICT DO NOTHING, so this
    # never disturbs the status of a repo a prior run already rowed. Placed
    # before the archived-seed apply so a dev repo that is also a config
    # `[archived]` seed is created here and archived by that loop in the same run.
    for repo in backfill.discovery_repos(cfg):
        store.upsert_project(str(repo), repo.name)

    # After indexing, because a path only worth archiving may not have had a
    # project row until this run created it -- and only for the paths this run
    # first saw. Re-asserting the config on every run would mean restoring one of
    # them in the panel came silently undone at the next index: config would be
    # overriding the user rather than seeding them.
    for archived in unseen_archived:
        row = store.project_by_path(archived)
        if row is not None:
            store.set_project_status(row["id"], "archived")

    # Auto-archive a project whose directory has vanished (spec:412) -- but only
    # the FIRST run we see it gone. `missing_archived_at` records that we acted,
    # so a later manual restore in the panel is not silently undone at the next
    # index. Iterates all rows (active + hidden + archived): a hidden project
    # that was deleted should leave the hidden drawer too, and stamping an
    # already-archived one is a harmless no-op that still protects a future restore.
    for project in store.projects(include_hidden=True):
        if project["missing_archived_at"] is not None:
            continue
        if not Path(project["path"]).exists():
            store.archive_missing(project["id"], now_epoch())

    # Last, because it can only match sessions this run has already written.
    stats.launches_linked = _link_background_launches(store)

    # Diagnostics is a reader of indexing, never a risk to it: a failure here
    # must not lose a scan that already succeeded. Indexing is the one thing
    # that must always work.
    try:
        store.record_index_run(
            dataclasses.asdict(stats),
            ran_at=now_epoch(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception:  # noqa: BLE001
        pass

    return stats


# `store.launches`/`store.sessions` are paged for the UI; correlation needs the
# whole set, so the limit is raised rather than a new query added.
_ALL = 1_000_000


def _link_background_launches(store: Store) -> int:
    """Fill in `session_id` for background launches, by unique `short_id` prefix.

    Terminal launches need nothing here: their UUID is pre-assigned, so
    `launches.session_id = sessions.id` holds the moment the session is written.
    `claude --bg` ignores `--session-id` and mints its own, so a background launch
    starts life with only the 8-hex handle it printed — which is exactly
    `session_id[:8]`.

    Eight hex characters is 2^32, and the candidate set is one project's sessions,
    so a collision is unlikely and not impossible. A **unique** prefix match is
    required and ambiguity leaves the row null, because binding a launch to the
    wrong session is worse than leaving it unlinked: the panel would then show a
    session Bridge did not start as one it did. Zero matches is equally ordinary —
    the session may not have written a transcript yet, or ever — and the launch
    stays visible as what it is.
    """
    linked = 0
    for project in store.projects(include_hidden=True):
        pid = project["id"]
        pending = [
            row
            for row in store.launches(pid, limit=_ALL)
            if row["short_id"] and not row["session_id"]
        ]
        if not pending:
            continue
        session_ids = [s["id"] for s in store.sessions(pid, limit=_ALL)]
        for row in pending:
            short = row["short_id"]
            matches = [sid for sid in session_ids if sid.startswith(short)]
            if len(matches) != 1:
                continue
            store.set_launch_session(row["id"], matches[0], short)
            linked += 1
    return linked


def _index_one(
    store: Store, path: Path, stats: IndexStats, aliases: dict[str, str]
) -> None:
    st = path.stat()
    prior = store.get_scan_state(str(path))
    start, prev = 0, None

    if prior is not None:
        if prior["size"] == st.st_size and prior["mtime"] == st.st_mtime:
            return  # unchanged; do not open
        if st.st_size >= prior["size"]:
            start = prior["parsed_offset"]
            prev = _rehydrate(store, prior["session_id"], str(path))

    result = scan(path, start_offset=start, prev=prev)

    stats.files_scanned += 1
    stats.lines_parsed += result.lines_parsed
    stats.parse_errors += result.parse_errors

    rec = result.record
    sid = rec.session_id if rec else (prior["session_id"] if prior else None)

    with store.transaction():
        store.set_scan_state(str(path), st.st_size, st.st_mtime, result.new_offset, sid)

        if rec is None:
            return  # no record to upsert

        # If this is an incremental scan with no new cwd, use the prior project path
        project_path = rec.project_path
        if not project_path and prior and prior["session_id"] == rec.session_id:
            # Fetch the existing session to get its project
            existing = store.session_row(rec.session_id)
            if existing:
                # Don't update project attribution, but do update the session record
                pid = existing["project_id"]
                store.upsert_session(rec, pid)
                stats.sessions_upserted += 1
                return

        if not project_path:
            return  # no resolvable project; nothing to attribute the session to
        # Exact match only: `~/Documents/projectX` and its `nested-app` child are
        # separate projects with separate mappings, so no prefix rewriting.
        project_path = aliases.get(project_path, project_path)
        pid = store.upsert_project(project_path, display_name(project_path))
        store.upsert_session(rec, pid)
        stats.sessions_upserted += 1


def _rehydrate(store: Store, session_id: str | None, path: str) -> SessionRecord | None:
    """Rebuild the accumulator so an incremental scan adds onto prior totals."""
    if not session_id:
        return None
    row = store.session_row(session_id)
    if row is None:
        return None
    return SessionRecord(
        session_id=row["id"],
        transcript_path=path,
        project_path=None,
        title=row["title"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        model=row["model"],
        effort=row["effort"],
        git_branch=row["git_branch"],
        user_msgs=row["user_msgs"],
        assistant_msgs=row["assistant_msgs"],
        last_prompt=row["last_prompt"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        tokens_cache_create=row["tokens_cache_create"],
        tokens_cache_read=row["tokens_cache_read"],
        sidechain_tokens=row["sidechain_tokens"],
        interrupted=bool(row["interrupted"]),
        # Without this the resumed scan has forgotten which response it already
        # counted, and recounts the one straddling the boundary.
        last_usage_request_id=row["last_usage_request_id"],
    )
