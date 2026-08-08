"""In-process change notifier: turns the SSE poll into push.

A monotonic revision guarded by a Condition. Producers `bump()` it; the SSE
loop `wait()`s on it instead of sleeping. The lost-wakeup guarantee lives in
`wait`: it compares `revision > since` UNDER the lock before blocking, so a
bump landing between a waiter reading `since` and calling `wait` is never slept
through.
"""

from __future__ import annotations

import threading


class ChangeNotifier:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._revision = 0

    @property
    def revision(self) -> int:
        with self._cond:
            return self._revision

    def bump(self) -> int:
        # O(1) on purpose: called from the event-loop thread by the async
        # /api/hooks handler, so it must never do I/O under the lock.
        with self._cond:
            self._revision += 1
            self._cond.notify_all()
            return self._revision

    def wait(self, since: int, timeout: float) -> int:
        with self._cond:
            if self._revision <= since:
                self._cond.wait(timeout)
            return self._revision
