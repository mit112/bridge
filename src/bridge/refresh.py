"""In-process refresh ownership for Bridge.

The server is the only process that indexes transcripts and writes the derived
database.  This coordinator adds one process-local gate so an explicit refresh
and the periodic worker cannot scan from the same stale scan-state boundary at
the same time.

**The 15s periodic reindex is not a duplicate of the watcher's 15s full
reconcile, and must not be collapsed into it.** It looks like one -- same
interval, same `reindex` call -- but the watcher fires `on_change` only when a
transcript's file set or an (mtime, size) pair actually moved, and four things
depend on a run happening even when nothing under the transcripts root did:

  * **Repo discovery.** `backfill.discovery_repos` cards a git repo under a
    discovery path that has no transcripts yet. Its input is `~/dev`, which the
    watcher does not watch, so a freshly cloned repo would never appear until
    something unrelated wrote a transcript.
  * **Vanished-project archiving.** The auto-archive pass stats every project
    row's directory. Deleting a project directory changes nothing inside the
    transcripts root, so nothing would ever fire the pass.
  * **`generation`, and with it the SSE full update.** `/events` sends a
    `full_update` frame only when `generation` moves, and `live_patch` strips
    `git` and `burn` from every card. A no-op reindex bumping `generation` is
    therefore the only thing that pushes changed git state to a connected tab
    when the change came from outside Claude.
  * **The watcher-failed fallback.** `serve` explicitly tolerates a watcher
    that cannot start, on the grounds that this loop still covers changes at
    its own interval. Remove the loop and that degradation becomes an outage.

A no-op run is cheap and is what makes those four honest; the cost worth
attacking is the run itself, not its cadence.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from bridge.config import Config
from bridge.indexer import IndexStats, reindex
from bridge.store import Store, now_epoch

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshStatus:
    generation: int = 0
    index_at: int | None = None
    server: str = "available"
    attempted_at: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class RefreshResult:
    completed: bool
    stats: IndexStats | None
    error: str | None
    status: RefreshStatus


class RefreshCoordinator:
    def __init__(
        self,
        store: Store,
        cfg: Config,
        reindex_fn: Callable[[Store, Config], IndexStats] = reindex,
        interval_s: float = 15.0,
        on_change: "Callable[[], None] | None" = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.reindex_fn = reindex_fn
        self.interval_s = interval_s
        self._on_change = on_change
        self._run_lock = threading.Lock()
        self._status_lock = threading.Lock()
        latest = store.latest_index_run()
        self._status = RefreshStatus(
            index_at=int(latest["ran_at"]) if latest is not None else None,
        )

    def status_snapshot(self) -> RefreshStatus:
        with self._status_lock:
            return self._status

    def run_once(self) -> RefreshResult:
        with self._run_lock:
            attempted_at = now_epoch()
            try:
                stats = self.reindex_fn(self.store, self.cfg)
            except Exception as exc:  # noqa: BLE001 - refresh must not kill serve
                message = _short_error(exc)
                with self._status_lock:
                    self._status = RefreshStatus(
                        generation=self._status.generation,
                        index_at=self._status.index_at,
                        server="unavailable",
                        attempted_at=attempted_at,
                        error=message,
                    )
                log.exception("periodic or explicit Bridge refresh failed")
                self._fire_on_change()
                return RefreshResult(False, None, message, self.status_snapshot())

            latest = self.store.latest_index_run()
            index_at = int(latest["ran_at"]) if latest is not None else attempted_at
            with self._status_lock:
                self._status = RefreshStatus(
                    generation=self._status.generation + 1,
                    index_at=index_at,
                    server="available",
                    attempted_at=attempted_at,
                    error=None,
                )
            self._fire_on_change()
            return RefreshResult(True, stats, None, self.status_snapshot())

    def run_periodic(self, stop_event: threading.Event) -> None:
        self.run_once()
        while not stop_event.wait(self.interval_s):
            self.run_once()

    def _fire_on_change(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:  # noqa: BLE001 - a notifier bump must not kill refresh
            log.exception("refresh on_change callback failed")


def _short_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return message[:240] or exc.__class__.__name__
