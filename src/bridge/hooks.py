"""Hook events from Claude Code, and the one state polling cannot see.

There is **no JSONL entry type that records a permission prompt**. Verified
against 2.1.220: transcript-tailing and the session registry both provably
cannot see that a session is sitting at a prompt waiting for a human. The best
transcript-only proxy anyone has managed is "a `tool_use` with no matching
`tool_result`, and an mtime under 60 s", which is a guess. Hooks or nothing.

`type: "http"` hooks let Claude Code POST straight to a FastAPI route with no
shim script, which also deletes the failure mode a shim would have: a handler
that waits on the response makes Claude Code visibly hang at "running hooks".

**This state is deliberately in-memory and deliberately not durable.** Hook
events are silently lost whenever Bridge is down, so persisting them would
manufacture a record with gaps nothing can detect. JSONL indexing and the
session registry stay the reconciliation source of truth; this is an overlay on
top of them, and it expires.
"""

import time
from dataclasses import dataclass, field

# `Notification` carries the distinction we want in `notification_type`, and
# unlike `PermissionRequest` it cannot block a turn. These are the values
# extracted from the 2.1.220 binary.
NEEDS_INPUT_TYPES = frozenset({"permission_prompt", "agent_needs_input", "idle_prompt"})

# The status a waiting session reports. It sits at the top of the attention
# ladder in `cards.LIVE_PRIORITY`.
NEEDS_INPUT = "needs_input"

# An overlay must not outlive its evidence. If Bridge misses the event that
# would clear a prompt -- a restart, a dropped POST -- the card would otherwise
# claim "waiting for you" forever. Ten minutes is long enough to survive a slow
# human and short enough that a stuck entry ages out on its own.
DEFAULT_TTL_S = 600


@dataclass
class HookState:
    """Which sessions are waiting on a human, as last reported by a hook."""

    ttl_s: float = DEFAULT_TTL_S
    _waiting: dict[str, float] = field(default_factory=dict)

    def record(self, event: dict, now: float | None = None) -> str | None:
        """Fold one hook event in. Returns the session id it applied to.

        Every failure mode here is "ignore it": a hook that raises would, at
        best, log noise in somebody's unrelated session. The route above this
        must always answer 200.
        """
        now = time.monotonic() if now is None else now
        if not isinstance(event, dict):
            return None
        session_id = event.get("session_id") or event.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            return None

        name = event.get("hook_event_name") or event.get("hookEventName") or ""
        if name == "Notification":
            kind = event.get("notification_type") or event.get("notificationType")
            if kind in NEEDS_INPUT_TYPES:
                self._waiting[session_id] = now
            else:
                # `agent_completed` and anything unrecognised mean the prompt,
                # if there was one, is no longer outstanding.
                self._waiting.pop(session_id, None)
        elif name in ("SessionStart", "SessionEnd"):
            # A session that just started is not waiting, and one that ended
            # cannot be. Both clear rather than set.
            self._waiting.pop(session_id, None)
        return session_id

    def is_waiting(self, session_id: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        seen = self._waiting.get(session_id)
        if seen is None:
            return False
        if now - seen > self.ttl_s:
            del self._waiting[session_id]
            return False
        return True

    def waiting_ids(self, now: float | None = None) -> set[str]:
        now = time.monotonic() if now is None else now
        return {sid for sid in list(self._waiting) if self.is_waiting(sid, now)}

    def forget(self, session_ids) -> None:
        """Drop sessions the liveness sensor can no longer see.

        The registry is the reconciliation source of truth: a session that is
        gone cannot be waiting, whatever the last hook said.
        """
        keep = set(session_ids)
        for sid in [s for s in self._waiting if s not in keep]:
            del self._waiting[sid]
