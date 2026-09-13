"""What moved in a repo between a handoff being written and being run.

A queued handoff states a branch and a commit as fact. Both were true when it
was written -- days ago, sometimes, and often on a branch that has since been
merged away. Nothing else in Bridge holds both ends of that gap: the fingerprint
the CLI captured in the project directory at authoring time, and the git state
the panel already probes now. Comparing the two is the whole module.

Two consumers share one comparison on purpose. `cards` renders it on the queued-
handoff panel, and `routes_handoffs` prepends it to the bytes a launch actually
runs. A second implementation of "has this rotted" would be free to disagree
with the first, on the one question where they must agree.

Silence is the default and it is load-bearing. A handoff captured before the
fingerprint existed, one written outside a git repo, and one whose repo the
panel cannot currently read all have nothing to say -- and say nothing, rather
than reporting a change they did not observe.
"""

from bridge.models import GitState

SHORT = 7


def compare(handoff_row, git: GitState | None) -> dict | None:
    """How the repo has moved since `handoff_row` was written, or None.

    `handoff_row` is anything subscriptable by column name -- a `sqlite3.Row`
    straight off `queued_handoffs`, or the dict `cards` builds from one.

    Only fields that were actually recorded are compared. A missing fingerprint
    is not evidence that nothing changed, so it produces no drift rather than a
    reassuring empty one.
    """
    if git is None or git.status != "ok":
        return None
    try:
        branch_then = handoff_row["created_branch"]
        head_then = handoff_row["created_head"]
    except (KeyError, IndexError):
        # A row from a database that predates the migration. Nothing recorded,
        # so nothing to compare.
        return None

    drift: dict = {}
    if branch_then and git.branch and branch_then != git.branch:
        drift["branch_then"] = branch_then
        drift["branch_now"] = git.branch
    if head_then and git.head and head_then != git.head:
        drift["head_then"] = head_then[:SHORT]
        drift["head_now"] = git.head[:SHORT]
    return drift or None


def describe(drift: dict) -> str:
    """One line of prose for a drift dict, shared by the panel and the preamble.

    Written so it reads correctly when only one of the two changed: a branch
    switch with no new commits, and new commits on the same branch, are both
    ordinary and must not render as half a sentence.
    """
    then = _side(drift.get("branch_then"), drift.get("head_then"))
    now = _side(drift.get("branch_now"), drift.get("head_now"))
    return f"written against {then}; the repo is now at {now}"


def _side(branch: str | None, head: str | None) -> str:
    if branch and head:
        return f"{branch} @ {head}"
    return branch or head or "an unrecorded state"


def preamble(drift: dict) -> str:
    """The block prepended to the prompt a drifted handoff launches.

    The point is not to fix the prompt -- nothing here knows what the right
    instruction would be -- but to stop the next session treating a stale
    premise as established fact. It is deliberately bracketed and deliberately
    ugly: a reader must be able to tell at a glance which words the authoring
    session wrote and which ones Bridge added.

    `launches.prompt` records the bytes that actually ran, preamble included, so
    prepending here never costs the audit trail its honesty.
    """
    return (
        f"[bridge] This prompt was {describe(drift)}. "
        "Verify the state it describes before acting on it.\n\n"
    )
