"""The in-process scheduler: claims and fires due `scheduled_runs` rows.

Runs on a daemon thread started by `bridge serve` (never by `create_app`, so
no test spawns it or fires a real session). `tick` is the unit under test;
`run_scheduler` is the thin loop `__main__.py` hands to `threading.Thread`.
"""

import logging

from bridge import launcher, schedspool
from bridge.firing import _fire_claimed_job
from bridge.store import now_epoch

log = logging.getLogger(__name__)

# How late a job may be and still fire. Past this it becomes `missed`.
#
# A job whose time passed while the panel was down should not launch a session
# at an unpredictable moment for work the user may have forgotten scheduling --
# the same refusal `schedspool.rebuild_if_empty`'s rule 5 makes after a database
# loss, and the reason `bridge launch` never spools. An hour is short enough
# that nothing fires unexpectedly and long enough that a laptop lid closed over
# lunch, or a restart during `bridge serve`, still runs what it owed.
CATCH_UP_SECONDS = 3600


def _retire_stale(store, cfg, when: int) -> None:
    """Mark every pending job past the catch-up window `missed`.

    Journal-first, and a journal failure leaves the row `pending` rather than
    marking it: `claim_one_due`'s window already refuses to fire it, so the
    only cost of retrying next tick is that the panel shows it as pending a
    little longer -- against a database loss silently replaying it as owed.
    """
    for id in store.overdue_pending_ids(when - CATCH_UP_SECONDS):
        try:
            schedspool.journal_status(id, "missed", when, cfg.spool_dir)
        except Exception:  # noqa: BLE001 - retried next tick; see above
            log.exception("failed to journal the missed scheduled run %r", id)
            continue
        if store.miss_pending(id, when):
            log.warning(
                "scheduled run %r was more than %ds overdue and was marked "
                "missed rather than fired", id, CATCH_UP_SECONDS,
            )


def tick(store, cfg, launch_fn=launcher.launch, now=None) -> int:
    """Claim and fire every due job, one at a time, until none are left.

    A job more than `CATCH_UP_SECONDS` late is retired as `missed` first, and
    the claim below is bounded by the same window so one can never slip
    through between the sweep and the claim.

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
    _retire_stale(store, cfg, when)
    while (row := store.claim_one_due(
        when, not_before=when - CATCH_UP_SECONDS
    )) is not None:
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
