"""In-process refresh ownership for Bridge.

The server is the only process that indexes transcripts and writes the derived
database.  This coordinator adds one process-local gate so an explicit refresh
and the periodic worker cannot scan from the same stale scan-state boundary at
the same time.
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
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.reindex_fn = reindex_fn
        self.interval_s = interval_s
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
            return RefreshResult(True, stats, None, self.status_snapshot())

    def run_periodic(self, stop_event: threading.Event) -> None:
        self.run_once()
        while not stop_event.wait(self.interval_s):
            self.run_once()


def _short_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return message[:240] or exc.__class__.__name__
