"""The spool is both an outbox and a journal.

**Outbox.** `bridge handoff` writes here whenever the server is unreachable.
Under the manual-`bridge serve` uptime model that is the normal path, not an
edge case, so it is treated as load-bearing rather than as a fallback.

**Journal.** A drained file is *moved* to `drained/`, never deleted. Handoffs
are the first data Bridge stores that cannot be regenerated from transcripts, so
Phase 1's "the database is a pure derived cache; delete it and re-index" is no
longer true of the `handoffs` table on its own. The retained files are what keep
it true of the system: they can rebuild the table, so `rm ~/.bridge/bridge.db`
stays a safe operation.

Any change that unlinks a spool file on successful drain forfeits that property.
`test_drained_files_are_retained_and_can_rebuild_the_table` exists to fail if
someone tries.
"""

import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bridge.models import Handoff
from bridge.registry import resolve_project

_FIELDS = frozenset(f.name for f in dataclasses.fields(Handoff))


@dataclass
class DrainStats:
    drained: int = 0
    bad: int = 0
    failed: int = 0
    skipped: int = 0


def _dirs(spool_dir: Path) -> tuple[Path, Path, Path]:
    live = Path(spool_dir)
    return live, live / "drained", live / "bad"


def write(h: Handoff, spool_dir: Path) -> Path:
    """Serialize one handoff durably enough that a reader never sees a partial file.

    The temp file is created in the *same* directory so `os.replace` is an
    atomic rename rather than a cross-filesystem copy, and its name is
    dot-prefixed so `pending()` cannot pick it up mid-write.
    """
    live, _, _ = _dirs(spool_dir)
    live.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dataclasses.asdict(h), indent=2, ensure_ascii=False)

    fd, tmp = tempfile.mkstemp(dir=live, prefix=f".{h.id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        final = live / f"{h.id}.json"
        os.replace(tmp, final)
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
        except Exception:  # noqa: BLE001 - one bad file must not stop the drain
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

    Phase 2 journals creations and not status changes, so a rebuilt table shows
    every recorded handoff as queued again. That is the right trade for
    recovering a wiped cache and the wrong one for routine use — hence the
    guard.
    """
    if store.handoff_count() > 0:
        return DrainStats(skipped=1)

    resolve = resolve or resolve_project
    _, drained_dir, _ = _dirs(spool_dir)
    stats = DrainStats()

    records: list[Handoff] = []
    if drained_dir.is_dir():
        for path in sorted(drained_dir.glob("*.json")):
            try:
                records.append(_load(path))
            except Exception:  # noqa: BLE001
                stats.bad += 1

    records.sort(key=lambda h: (h.created_at, h.id))
    for h in records:
        try:
            store.create_handoff(h, resolve(store, h.project_path))
            stats.drained += 1
        except Exception:  # noqa: BLE001
            stats.failed += 1

    live = drain(store, spool_dir, resolve)
    stats.drained += live.drained
    stats.bad += live.bad
    stats.failed += live.failed
    return stats
