import asyncio, threading, time
from bridge.notify import ChangeNotifier

def test_bump_increments_and_returns_monotonic_revisions():
    n = ChangeNotifier()
    assert n.revision == 0
    assert n.bump() == 1
    assert n.bump() == 2
    assert n.revision == 2

# --- waiting --------------------------------------------------------------
#
# `wait_async` is the only wait: the SSE stream awaits on the event loop
# instead of pinning a threadpool worker. `bump()` still arrives from
# watcher/indexer THREADS, which is the interesting half: it has to cross back
# into the loop to wake everyone parked there.

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

def test_two_async_waiters_both_wake_on_one_bump():
    # Fan-out, not hand-off: every parked SSE connection has to see the same
    # bump. Waking only the first waiter would leave the second sleeping out
    # its fallback timeout with a stale frame on screen.
    n = ChangeNotifier()

    async def scenario():
        tasks = [asyncio.ensure_future(n.wait_async(since=0, timeout=5.0))
                 for _ in range(2)]
        await asyncio.sleep(0.05)
        threading.Thread(target=n.bump).start()
        start = time.monotonic()
        revs = await asyncio.gather(*tasks)
        return revs, time.monotonic() - start

    revs, elapsed = asyncio.run(scenario())
    assert revs == [1, 1]
    assert elapsed < 2.0, "a waiter slept out the fallback instead of waking"
