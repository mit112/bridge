"""The spool is both an outbox and a journal.

**Outbox.** `bridge handoff` writes here whenever the server is unreachable.
Under the manual-`bridge serve` uptime model that is the normal path, not an
edge case, so it is treated as load-bearing rather than as a fallback.

**Journal.** A drained file is *moved* to `drained/`, never deleted. Handoffs
are the first data Bridge stores that cannot be regenerated from transcripts, so
the original "the database is a pure derived cache; delete it and re-index" is no
longer true of the `handoffs` table on its own. The retained files are what keep
it true of the system: they can rebuild the table, so `rm ~/.bridge/bridge.db`
stays a safe operation. Status *changes* are journalled alongside creations, so a
rebuild restores the state each handoff ended in rather than showing every one of
them as queued again.

Any change that unlinks a spool file on successful drain forfeits that property.
`test_drained_files_are_retained_and_can_rebuild_the_table` exists to fail if
someone tries.
"""

import dataclasses
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bridge.models import Handoff
from bridge.registry import resolve_project

log = logging.getLogger(__name__)

_FIELDS = frozenset(f.name for f in dataclasses.fields(Handoff))

# What distinguishes a status record from a handoff record inside `drained/`.
# The two now share a directory, so each loader must skip the other's files:
# parsing a status record as a handoff would quarantine a perfectly good file.
STATUS_SUFFIX = ".status.json"


@dataclass
class DrainStats:
    drained: int = 0
    bad: int = 0
    failed: int = 0
    skipped: int = 0
    statuses: int = 0


@dataclass(frozen=True)
class _Status:
    """One journalled handoff status change. `at` is what orders the replay."""

    handoff_id: str
    status: str
    at: int


def _dirs(spool_dir: Path) -> tuple[Path, Path, Path]:
    live = Path(spool_dir)
    return live, live / "drained", live / "bad"


def write(h: Handoff, spool_dir: Path) -> Path:
    """Queue a handoff for a server that is not answering. The outbox path."""
    live, _, _ = _dirs(spool_dir)
    return _atomic_write(dataclasses.asdict(h), h.id, live)


def journal(h: Handoff, spool_dir: Path) -> Path:
    """Record a handoff the server accepted directly, without it ever spooling.

    A live POST never passes through the outbox, so without this the journal
    would only contain handoffs captured while the panel was *down* — and
    `rm ~/.bridge/bridge.db` would lose every one captured while it was up.
    Writing straight into `drained/` keeps the journal complete: it means
    "already in the database", which is exactly true here.
    """
    _, drained_dir, _ = _dirs(spool_dir)
    return _atomic_write(dataclasses.asdict(h), h.id, drained_dir)


def journal_status(handoff_id: str, status: str, at: int, spool_dir: Path) -> Path:
    """Record a handoff's *status change*, so a rebuild replays where it ended up.

    Creations alone were journalled at first, which cost nothing while nothing
    consumed a handoff. Once ▶ exists it costs the panel's most load-bearing signal:
    launch a prompt, `rm ~/.bridge/bridge.db`, re-index, and a creations-only
    journal puts the prompt you already ran back at the top of the dashboard.

    The `.status.json` suffix is what keeps these files out of the creation
    loader's glob, and the epoch separates successive changes to one handoff.

    The epoch is in seconds, so two *different* statuses for the same handoff
    inside one second write the same filename and the later one wins. That is
    accepted rather than fixed: `status` is a bare string, `_atomic_write`
    resolves `<stem>.json` against the directory, and putting it in the filename
    would turn a status value into a path. The property that matters survives a
    collision either way — every status that can collide here is terminal and
    non-queued, so a launched prompt still never replays as queued.
    """
    # Checked here as well as inside `_atomic_write`: the stem below is
    # *composed*, so `""` or `"."` would pass the composed check as
    # `.1000.status` and still write a file named after nothing.
    check_record_id(handoff_id)
    _, drained_dir, _ = _dirs(spool_dir)
    return _atomic_write(
        {"handoff_id": handoff_id, "status": status, "at": at},
        f"{handoff_id}.{at}.status",
        drained_dir,
    )


def fsync_dir(directory: Path) -> None:
    """fsync a directory entry after an `os.replace` into it.

    `os.replace` is atomic -- a reader never sees a half-written file -- but
    the RENAME ITSELF is only durable once the directory entry pointing at it
    is on disk. fsyncing the file being replaced covers its contents, not the
    directory's own metadata; without this, a power loss right after
    `os.replace` can still lose the rename on some filesystems, even though
    the file's bytes are safe. Shared by every atomic writer in this module
    and by `launcher.write_prompt_file`, which has the identical shape.
    """
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def check_record_id(stem: str) -> None:
    """Reject a filename stem that would escape `directory` once joined.

    Every stem here derives from a handoff id, and a handoff id arrives over
    the wire on `POST /api/handoff`. `directory / f"{stem}.json"` treats a
    leading `/` as an absolute path and `..` as a parent, so an unchecked id
    turns a journal write into a write anywhere the panel's user can reach.
    Ids are minted as uuid4 in practice; this only rules out the characters
    that carry path meaning, so existing non-uuid ids keep working.
    """
    if not stem or stem == ".":
        raise ValueError(f"unusable record id {stem!r}")
    if ".." in stem or any(c in stem for c in ("/", "\\", "\x00")):
        raise ValueError(f"record id {stem!r} may not contain a path separator")


def _atomic_write(payload: dict, stem: str, directory: Path) -> Path:
    """Serialize one record durably enough that a reader never sees a partial file.

    The temp file is created in the *same* directory so `os.replace` is an
    atomic rename rather than a cross-filesystem copy, and its name is
    dot-prefixed so `pending()` cannot pick it up mid-write.
    """
    check_record_id(stem)
    live = directory
    live.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, ensure_ascii=False)

    fd, tmp = tempfile.mkstemp(dir=live, prefix=f".{stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        final = live / f"{stem}.json"
        os.replace(tmp, final)
        fsync_dir(live)
        return final
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def pending(spool_dir: Path) -> list[Path]:
    live, _, _ = _dirs(spool_dir)
    if not live.is_dir():
        return []
    return sorted(p for p in live.glob("*.json") if p.is_file())


def pending_count(spool_dir: Path) -> int:
    return len(pending(spool_dir))


def _load(path: Path) -> Handoff:
    """Parse one spool file, tolerating unknown keys but not missing ones.

    Unknown keys are ignored so a file written by a newer Bridge still drains;
    a missing `id`, `project_path` or `next_prompt` is unrecoverable and the
    file belongs in `bad/`.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: top level is {type(data).__name__}, not object")
    h = Handoff(**{k: v for k, v in data.items() if k in _FIELDS})
    if not h.id or not h.project_path or not h.next_prompt:
        raise ValueError(f"{path.name}: missing id, project_path or next_prompt")
    return h


def _load_status(path: Path) -> _Status:
    """Parse one status record, which is unusable without all three fields.

    `at` orders the replay, so a record missing it cannot be placed in the
    history at all; there is no defensible default. Straight to `bad/`.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: top level is {type(data).__name__}, not object")
    handoff_id, status, at = data.get("handoff_id"), data.get("status"), data.get("at")
    if not handoff_id or not status or not isinstance(at, int):
        raise ValueError(f"{path.name}: missing handoff_id, status or at")
    return _Status(handoff_id, status, at)


def _quarantine(path: Path, bad_dir: Path) -> None:
    bad_dir.mkdir(parents=True, exist_ok=True)
    os.replace(path, bad_dir / path.name)


def drain(store, spool_dir: Path, resolve=None) -> DrainStats:
    """Ingest every pending file into the store, retaining each one afterwards.

    Files are inserted in `created_at` order because `create_handoff` supersedes:
    replaying out of order would leave an older prompt queued and the newest
    superseded. A file that cannot be parsed is quarantined and never blocks the
    rest of the run; a file that fails to *insert* is left in place, because that
    is a transient database problem and the next boot should retry it.
    """
    resolve = resolve or resolve_project
    _, drained_dir, bad_dir = _dirs(spool_dir)
    stats = DrainStats()

    parsed: list[tuple[Handoff, Path]] = []
    for path in pending(spool_dir):
        try:
            parsed.append((_load(path), path))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the drain
            log.warning("quarantining unparsable spool file %s: %s", path.name, exc)
            _quarantine(path, bad_dir)
            stats.bad += 1

    parsed.sort(key=lambda pair: (pair[0].created_at, pair[0].id))

    for h, path in parsed:
        try:
            store.create_handoff(h, resolve(store, h.project_path))
        except Exception:  # noqa: BLE001 - leave it spooled and retry next boot
            stats.failed += 1
            continue
        drained_dir.mkdir(parents=True, exist_ok=True)
        os.replace(path, drained_dir / path.name)
        stats.drained += 1

    return stats


def rebuild_if_empty(store, spool_dir: Path, resolve=None) -> DrainStats:
    """Replay the journal, but only into an empty `handoffs` table.

    The guard is the whole point. Replaying unconditionally would resurrect a
    consumed handoff as queued on every index, so a prompt you had already used
    would reappear on the card forever. Guarded, this only runs after genuine
    database loss.

    Creations and status changes are loaded separately and applied in that
    order: every creation first, then every status record sorted by its `at`, so
    a queued → superseded → consumed history replays to the state it ended in
    rather than to whatever order the glob returned. That makes recovery
    *faithful*; it does not make replay routine. The empty-table guard above is
    what keeps those two things apart, and any design that replays statuses onto
    a live table has forfeited it.
    """
    if store.handoff_count() > 0:
        return DrainStats(skipped=1)

    resolve = resolve or resolve_project
    _, drained_dir, bad_dir = _dirs(spool_dir)
    stats = DrainStats()

    records: list[Handoff] = []
    statuses: list[_Status] = []
    if drained_dir.is_dir():
        for path in sorted(drained_dir.glob("*.json")):
            if path.name.endswith(STATUS_SUFFIX):
                try:
                    statuses.append(_load_status(path))
                except Exception as exc:  # noqa: BLE001 - one bad record cannot stop the replay
                    log.warning(
                        "quarantining unparsable status record %s: %s", path.name, exc
                    )
                    _quarantine(path, bad_dir)
                    stats.bad += 1
                continue
            try:
                records.append(_load(path))
            except Exception:  # noqa: BLE001
                stats.bad += 1

    records.sort(key=lambda h: (h.created_at, h.id))
    # One transaction for every creation record: a record that fails to
    # insert used to be counted in `stats.failed` and then left behind --
    # whatever DID land made `handoff_count() > 0` true, so the next rebuild
    # hit the guard above and skipped entirely, and the failed record's
    # creation file was never quarantined either, so it was never retried,
    # permanently. Rolling the whole batch back on any failure keeps the
    # table empty, so the guard stays honest and a fixed retry restores
    # everything -- including statuses and the live outbox drain below,
    # which are skipped this attempt rather than applied against a
    # creation set that only partially exists.
    try:
        with store.transaction():
            for h in records:
                store.create_handoff(h, resolve(store, h.project_path))
                stats.drained += 1
    except Exception as exc:  # noqa: BLE001 - reported, not raised; see below
        log.exception(
            "handoff rebuild failed partway through and rolled back "
            "(%d of %d records restored before the failure): %s",
            stats.drained, len(records), exc,
        )
        return DrainStats(failed=1)

    # Statuses last, in `at` order. A creation queues, and `create_handoff`
    # supersedes as it goes, so applying a status before every creation is in
    # place would let a later insert overwrite the state we just restored.
    for s in sorted(statuses, key=lambda s: (s.at, s.handoff_id)):
        store.set_handoff_status(s.handoff_id, s.status)
        stats.statuses += 1

    live = drain(store, spool_dir, resolve)
    stats.drained += live.drained
    stats.bad += live.bad
    stats.failed += live.failed
    return stats
