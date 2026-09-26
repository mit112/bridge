"""The relative-time filters every "N ago" on the panel renders through.

Nothing else pinned them: the whole suite stayed green with `ago` returning an
empty string, because every template that uses it only ever asserted on the
surrounding markup.
"""

from datetime import datetime, timedelta, timezone

from bridge.filters import _ago, _ago_epoch
from bridge.store import now_epoch


def _iso(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_ago_renders_minutes_hours_and_days_from_an_iso_timestamp():
    assert _ago(_iso(5 * 60 + 10)) == "5m"
    assert _ago(_iso(2 * 3600 + 10)) == "2h"
    assert _ago(_iso(3 * 86400 + 10)) == "3d"


def test_ago_is_empty_for_a_timestamp_it_cannot_read():
    assert _ago(None) == ""
    assert _ago("not a timestamp") == ""


def test_ago_epoch_matches_ago_and_never_goes_negative():
    assert _ago_epoch(now_epoch() - 2 * 3600 - 10) == "2h"
    # A write racing the render can land a second in the future.
    assert _ago_epoch(now_epoch() + 5) == "0m"
    assert _ago_epoch(None) == ""
