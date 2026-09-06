"""In-process change notifier: turns the SSE poll into push.

A monotonic revision guarded by a Condition. Producers `bump()` it; the SSE
loop waits on it instead of sleeping. The lost-wakeup guarantee lives in the
waits: they compare `revision > since` UNDER the lock before blocking, so a
bump landing between a waiter reading `since` and calling wait is never slept
through.

Two waits, one revision. `wait()` is the threading version, for callers on a
worker thread. `wait_async()` is for callers on an event loop: it parks on an
`asyncio.Event` bound to the loop that registered it, so an idle SSE
connection costs an awaited future instead of a pinned threadpool worker.
`bump()` still runs from any thread -- watcher, indexer, or the loop itself --
and wakes the async side through `loop.call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger(__name__)


class ChangeNotifier:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._revision = 0
        # (loop, event) pairs, one per parked `wait_async` caller. Guarded by
        # `_cond` so registration and the revision check are one atomic step.
        self._async_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    @property
    def revision(self) -> int:
        with self._cond:
            return self._revision

    def bump(self) -> int:
        # O(1) on purpose: called from the event-loop thread by the async
        # /api/hooks handler, so it must never do I/O under the lock.
        with self._cond:
            self._revision += 1
            revision = self._revision
            self._cond.notify_all()
            waiters = list(self._async_waiters)
        # Outside the lock: `call_soon_threadsafe` takes the loop's own lock,
        # and a bump must never hold `_cond` across another lock.
        for loop, event in waiters:
            if loop.is_closed():
                # The waiter's loop shut down without running its own
                # `finally`. Drop the stale entry rather than retrying it on
                # every bump for the life of the process.
                with self._cond:
                    self._async_waiters.discard((loop, event))
                continue
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError as exc:
                # Closed between the check and the call. Nothing to recover,
                # but a bump that silently failed to wake a waiter is exactly
                # the class of bug that looks like "the UI froze".
                log.debug("notifier could not wake an async waiter: %s", exc)
        return revision

    def wait(self, since: int, timeout: float) -> int:
        with self._cond:
            if self._revision <= since:
                self._cond.wait(timeout)
            return self._revision

    async def wait_async(self, since: int, timeout: float) -> int:
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        entry = (loop, event)
        with self._cond:
            if self._revision > since:
                return self._revision
            self._async_waiters.add(entry)
        waiter = asyncio.ensure_future(event.wait())
        try:
            # `asyncio.wait` RETURNS on timeout instead of raising, so the
            # fallback-timeout path stays ordinary control flow.
            await asyncio.wait({waiter}, timeout=timeout)
        finally:
            waiter.cancel()
            with self._cond:
                self._async_waiters.discard(entry)
        with self._cond:
            return self._revision
