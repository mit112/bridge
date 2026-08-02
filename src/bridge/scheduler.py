"""The in-process scheduler: claims and fires due `scheduled_runs` rows.

Runs on a daemon thread started by `bridge serve` (never by `create_app`, so
no test spawns it or fires a real session). `tick` is the unit under test;
`run_scheduler` is the thin loop `__main__.py` hands to `threading.Thread`.
"""

import logging

from bridge import launcher
from bridge.api import _fire_claimed_job
from bridge.store import now_epoch

log = logging.getLogger(__name__)


def tick(store, cfg, launch_fn=launcher.launch, now=None) -> int:
    """Claim and fire every due job, one at a time, until none are left.

    `_fire_claimed_job` catches `launcher.LaunchError` and records a terminal
    status, so the ordinary "launch refused" case never reaches here. But it
    does NOT catch everything: `launcher.launch` can raise a bare `OSError`
    out of `write_prompt_file` (disk full, permission denied) before any
    `LaunchError` boundary, `store.create_launch` can raise a sqlite error,
    and a caller-supplied `launch_fn` (this scheduler's whole injection point)
    can raise anything at all. Any of those would otherwise escape this loop
    mid-run and strand every job still due behind the one that broke, so each
    claimed job gets its own guard: the claim already moved it to `launching`,
    reconcile_launching cleans it up on the next boot, and the next iteration
    still gets a chance at whatever else is due right now.
    """
    fired = 0
    when = now if now is not None else now_epoch()
    while (row := store.claim_one_due(when)) is not None:
        try:
            _fire_claimed_job(store, cfg, row, launch_fn)
        except Exception:
            log.exception(
                "scheduled run %r raised while firing; left 'launching' "
                "for reconcile_launching to resolve", row["id"],
            )
            continue
        fired += 1
    return fired


def run_scheduler(store, cfg, stop_event, launch_fn=launcher.launch, interval=30):
    """Tick every `interval` seconds until `stop_event` is set.

    A bad tick must never kill the daemon -- there is no supervisor to
    restart it -- so the only thing allowed to end this loop is `stop_event`.
    """
    while not stop_event.wait(interval):
        try:
            tick(store, cfg, launch_fn)
        except Exception:
            log.exception("scheduler tick failed")
