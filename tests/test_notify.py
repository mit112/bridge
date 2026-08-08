import threading, time
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
