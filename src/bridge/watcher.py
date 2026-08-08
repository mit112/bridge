"""Poll the transcripts dir for changes and fire a debounced callback.

Stdlib-only (no watchfiles). Stats every *.jsonl under `root` each `poll_s`;
appends to existing transcripts do NOT bump the parent dir mtime, so it must
stat files, not dirs. On a detected change it waits for a `quiet_s` lull before
firing `on_change` once, coalescing a burst of writes into one reindex.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class FileWatcher:
    def __init__(
        self, root: Path, on_change: Callable[[], None],
        poll_s: float = 0.5, quiet_s: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._root = Path(root)
        self._on_change = on_change
        self._poll_s = poll_s
        self._quiet_s = quiet_s
        self._clock = clock
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _snapshot(self) -> dict[str, tuple[float, int]]:
        out: dict[str, tuple[float, int]] = {}
        try:
            for f in self._root.rglob("*.jsonl"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                out[str(f)] = (st.st_mtime, st.st_size)
        except OSError:
            pass
        return out

    def _run(self) -> None:
        last = self._snapshot()
        pending_since: float | None = None
        while not self._stop.wait(self._poll_s):
            current = self._snapshot()
            if current != last:
                last = current
                pending_since = self._clock()          # (re)start the quiet window
                continue
            if pending_since is not None and self._clock() - pending_since >= self._quiet_s:
                pending_since = None
                try:
                    self._on_change()
                except Exception:  # noqa: BLE001 - a bad reindex must not kill the watcher
                    log.exception("file watcher on_change failed")
