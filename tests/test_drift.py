"""A handoff's fingerprint, and what it means once the repo has moved.

The property under test throughout is an ASYMMETRY. "Nothing was recorded" and
"nothing changed" must never render as the same thing: the first is ignorance
and the second is a claim, and a panel that reports the second from the first is
telling the user a stale prompt is safe to run.
"""

import pytest

from bridge import drift
from bridge.models import GitState, Handoff

OLD = "672c726aa11122223333444455556666aaaabbbb"
NEW = "efb7759bb11122223333444455556666aaaabbbb"


def row(**kw) -> dict:
    base = {"created_branch": "feat/x", "created_head": OLD}
    base.update(kw)
    return base


def ok(**kw) -> GitState:
    base = {"status": "ok", "branch": "feat/x", "head": OLD}
    base.update(kw)
    return GitState(**base)


# --- silence is not agreement ------------------------------------------------


def test_a_handoff_with_no_fingerprint_reports_no_drift():
    """Every handoff captured before the fingerprint existed is this case, and
    so is every one from a directory that is not a repo. None of them are
    evidence that the repo stood still."""
    assert drift.compare(row(created_branch=None, created_head=None), ok()) is None


def test_a_row_predating_the_migration_reports_no_drift_rather_than_raising():
    """A `sqlite3.Row` from a database that never had these columns raises on
    subscript rather than returning None, and a card build must not 500 on it."""
    assert drift.compare({}, ok()) is None


@pytest.mark.parametrize("status", ["not_a_repo", "unavailable"])
def test_an_unreadable_repo_reports_no_drift(status):
    """`unavailable` means the probe failed. Reporting "nothing moved" from a
    failed read asserts something nobody observed.

    The state carries a branch and head that DIFFER from the fingerprint, on
    purpose. With the defaults (`branch=None`, `head=None`) this passes with the
    status check deleted -- the comparison finds nothing to compare and returns
    None for the wrong reason. Populated, the status check is the only thing
    that can produce None.
    """
    unreadable = GitState(status=status, branch="main", head=NEW)
    assert drift.compare(row(), unreadable) is None


def test_an_unchanged_repo_reports_no_drift():
    assert drift.compare(row(), ok()) is None


# --- what actually moved -----------------------------------------------------


def test_a_new_commit_on_the_same_branch_is_drift():
    moved = drift.compare(row(), ok(head=NEW))
    assert moved == {"head_then": OLD[:7], "head_now": NEW[:7]}
    assert "branch_then" not in moved


def test_a_branch_switch_with_no_new_commit_is_drift():
    moved = drift.compare(row(), ok(branch="main"))
    assert moved == {"branch_then": "feat/x", "branch_now": "main"}


def test_both_moving_reports_both():
    moved = drift.compare(row(), ok(branch="main", head=NEW))
    assert set(moved) == {"branch_then", "branch_now", "head_then", "head_now"}


# --- the sentence stays whole ------------------------------------------------


def test_describe_reads_correctly_when_only_the_commit_moved():
    text = drift.describe(drift.compare(row(), ok(head=NEW)))
    assert text == f"written against {OLD[:7]}; the repo is now at {NEW[:7]}"


def test_describe_reads_correctly_when_only_the_branch_moved():
    text = drift.describe(drift.compare(row(), ok(branch="main")))
    assert text == "written against feat/x; the repo is now at main"


def test_describe_names_both_sides_when_both_moved():
    text = drift.describe(drift.compare(row(), ok(branch="main", head=NEW)))
    assert text == f"written against feat/x @ {OLD[:7]}; the repo is now at main @ {NEW[:7]}"


def test_the_preamble_is_marked_as_bridges_own_words_and_ends_in_a_blank_line():
    """A reader has to be able to tell at a glance which sentences the authoring
    session wrote and which ones Bridge added, and the prompt below must not be
    glued onto the end of the warning."""
    text = drift.preamble(drift.compare(row(), ok(head=NEW)))
    assert text.startswith("[bridge]")
    assert text.endswith("\n\n")


# --- the fingerprint survives the journal ------------------------------------


def test_the_fingerprint_round_trips_through_the_spool_journal(tmp_path):
    """The failure mode this whole feature is one `rm` away from.

    The journal is what survives `rm ~/.bridge/bridge.db`. A fingerprint that
    reached the database but not the journal would vanish on the rebuild that is
    supposed to restore it -- silently, and only for handoffs written before the
    rebuild, which is the hardest possible shape to notice.
    """
    from bridge import spool

    h = Handoff(id="h-fp", project_path="/p", next_prompt="go",
                created_branch="feat/x", created_head=OLD,
                created_dirty=3, created_ahead=20)
    spool.journal(h, tmp_path)

    back = spool._load(next((tmp_path / "drained").glob("*.json")))
    assert back.created_branch == "feat/x"
    assert back.created_head == OLD
    assert back.created_dirty == 3
    assert back.created_ahead == 20
