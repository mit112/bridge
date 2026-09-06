import asyncio, threading, time
from bridge.notify import ChangeNotifier

def test_bump_increments_and_returns_monotonic_revisions():
    n = ChangeNotifier()
    assert n.revision == 0
    assert n.bump() == 1
    assert n.bump() == 2
    assert n.revision == 2

def test_wait_returns_immediately_when_revision_already_ahead():
    # The lost-wakeup property as a PURE predicate (no timing race): a bump that
    # already happened before wait() is never slept through.
    n = ChangeNotifier()
    n.bump()                     # revision 1
    start = time.monotonic()
    got = n.wait(since=0, timeout=5.0)
    assert got == 1
    assert time.monotonic() - start < 0.5, "wait blocked despite revision > since"

def test_wait_times_out_when_no_bump():
    n = ChangeNotifier()
    start = time.monotonic()
    got = n.wait(since=0, timeout=0.2)
    assert got == 0
    assert time.monotonic() - start >= 0.2

def test_a_bump_wakes_a_blocked_waiter():
    n = ChangeNotifier()
    woke = {}
    def waiter():
        woke["rev"] = n.wait(since=0, timeout=5.0)
    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    n.bump()
    t.join(timeout=2.0)
    assert woke["rev"] == 1

def test_two_waiters_both_wake_on_one_bump():
    n = ChangeNotifier()
    revs = []
    lock = threading.Lock()
    def waiter():
        r = n.wait(since=0, timeout=5.0)
        with lock:
            revs.append(r)
    ts = [threading.Thread(target=waiter) for _ in range(2)]
    for t in ts: t.start()
    time.sleep(0.05)
    n.bump()
    for t in ts: t.join(timeout=2.0)
    assert revs == [1, 1]

# --- the async side -------------------------------------------------------
#
# The SSE stream awaits on the event loop instead of pinning a threadpool
# worker, so the same revision has to be waitable from a coroutine. `bump()`
# still arrives from watcher/indexer THREADS, which is the interesting half:
# it has to cross back into the loop to wake anyone parked there.

def test_wait_async_returns_immediately_when_revision_already_ahead():
    n = ChangeNotifier()
    n.bump()
    start = time.monotonic()
    got = asyncio.run(n.wait_async(since=0, timeout=5.0))
    assert got == 1
    assert time.monotonic() - start < 0.5, "wait_async blocked despite revision > since"

def test_wait_async_times_out_when_no_bump():
    n = ChangeNotifier()
    start = time.monotonic()
    got = asyncio.run(n.wait_async(since=0, timeout=0.2))
    assert got == 0
    assert time.monotonic() - start >= 0.2

def test_a_bump_from_another_thread_wakes_an_async_waiter():
    n = ChangeNotifier()

    async def scenario():
        # Registered under the same lock the revision check takes, so the bump
        # below either lands before the check (returns at once) or wakes the
        # waiter -- never in between.
        task = asyncio.ensure_future(n.wait_async(since=0, timeout=5.0))
        await asyncio.sleep(0.05)
        threading.Thread(target=n.bump).start()
        start = time.monotonic()
        got = await task
        return got, time.monotonic() - start

    got, elapsed = asyncio.run(scenario())
    assert got == 1
    assert elapsed < 2.0, "the waiter slept out the fallback instead of waking"

def test_wait_async_deregisters_itself_so_waiters_do_not_accumulate():
    # A leaked waiter is unbounded memory plus a `call_soon_threadsafe` per
    # bump per dead connection -- the cost the connection cap exists to bound.
    n = ChangeNotifier()

    async def scenario():
        await n.wait_async(since=0, timeout=0.01)
        n.bump()
        await n.wait_async(since=0, timeout=0.01)

    asyncio.run(scenario())
    assert n._async_waiters == set()

def test_async_and_sync_waiters_both_wake_on_one_bump():
    # Both APIs stay live at once: nothing that still calls `wait()` breaks
    # because the stream moved to `wait_async()`.
    n = ChangeNotifier()
    sync_result = {}

    def sync_waiter():
        sync_result["rev"] = n.wait(since=0, timeout=5.0)

    async def scenario():
        task = asyncio.ensure_future(n.wait_async(since=0, timeout=5.0))
        t = threading.Thread(target=sync_waiter)
        t.start()
        await asyncio.sleep(0.05)
        n.bump()
        got = await task
        t.join(timeout=2.0)
        return got

    assert asyncio.run(scenario()) == 1
    assert sync_result["rev"] == 1
