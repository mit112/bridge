"""What the diagnostics surface reports, and which of it counts as degraded.

Extracted from `create_app` because only `collect` needs the app's
collaborators at all -- `attention_items` and `needs_attention` are pure
functions of the dict `collect` returns, and were closures purely by accident
of where they were written.

`needs_attention` defers to `attention_items` truthiness rather than
re-checking the same conditions, so the top-of-page banner and the section
below it cannot disagree the next time a condition is added to only one.
"""

from dataclasses import asdict

from bridge import __version__, agents, spool
from bridge.config import Config
from bridge.models import AgentsState
from bridge.store import Store


def collect(
    store: Store,
    cfg: Config,
    update_checker,
    state: AgentsState | None = None,
) -> dict:
    """Everything the diagnostics view shows, as plain data.

    Shared by the JSON route and the HTML one so the two can never disagree
    about what is wrong.
    """
    run = store.latest_index_run()
    last_index = dict(run) if run is not None else None
    # A fresh install has no runs at all; the route must answer, not 500.
    parse_errors = int((last_index or {}).get("parse_errors") or 0)

    live = agents.probe() if state is None else state
    return {
        "version": __version__,
        "last_index": last_index,
        "parse_errors": parse_errors,
        "spool_depth": spool.pending_count(cfg.spool_dir),
        "live": live.status,
        "running_sessions": sum(
            1 for s in live.sessions if not agents.is_terminal(s.status)
        ),
        # Recorded so a future schema drift is a diagnosis rather than a
        # bisect: which sensor answered, and what version it reported.
        "live_source": live.source,
        "claude_version": live.version,
        "queued_handoffs": store.queued_handoff_count(),
        "update": asdict(update_checker.snapshot()),
    }

def attention_items(diag: dict) -> list[dict]:
    """The one place that decides which checks are failing/degraded, and
    what to say about each. Turns each into plain language: what it means
    and what to do about it. Presented under "Needs attention" so a fresh
    install is not led anywhere -- this is display-only grouping of the
    same `collect()` dict, never a new probe.

    `needs_attention` below defers to this list's truthiness rather than
    re-checking the same three conditions itself: two independent copies
    of "what counts as degraded" would let the top-of-page banner and
    this section silently disagree the next time a condition is added to
    only one of them.
    """
    items = []
    if diag["parse_errors"]:
        items.append({
            "label": "Parse errors during indexing",
            "cause": f"{diag['parse_errors']} line(s) in session files "
                     "failed to parse during the last index run.",
            "next_action": "Re-run indexing (POST /api/refresh) and check "
                     "the Bridge server log for the file and line that "
                     "failed; malformed JSONL lines are skipped, not fatal.",
        })
    if diag["spool_depth"]:
        items.append({
            "label": "Handoffs stuck in the spool",
            "cause": f"{diag['spool_depth']} handoff file(s) are queued "
                     "in the spool directory and have not been drained.",
            "next_action": "Confirm the spool drain process is running; "
                     "files remain in spool_dir until Bridge successfully "
                     "drains them.",
        })
    if diag["live"] == "unavailable":
        items.append({
            "label": "Liveness sensor unavailable",
            "cause": f"The {diag['live_source']} sensor could not "
                     "determine which Claude sessions are running.",
            "next_action": "Check that Claude Code's session registry "
                     "(or subprocess probe) is reachable on this machine, "
                     "then reload Diagnostics.",
        })
    return items

def needs_attention(diag: dict) -> bool:
    """A permanent "diagnostics" link would train the eye to ignore it."""
    return bool(attention_items(diag))
