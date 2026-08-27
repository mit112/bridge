"""Read-only liveness sensor. Never signals a session, never supervises one.

Two sensors, one shape. `~/.claude/sessions/<pid>.json` is the **primary**: it
is what `claude agents --json` is a view over, it is ~3000x cheaper (0.1 ms
against a measured 250-760 ms for the subprocess), and it carries five fields
the subprocess strips -- `statusUpdatedAt`, `updatedAt`, `version`, `procStart`
and `entrypoint`. The first of those is the staleness input the live band's
hysteresis needs and the third is the schema-drift signal diagnostics reports.
Reading it is consistent with Bridge's existing bet: it already depends
wholesale on `~/.claude/projects/**/*.jsonl` internals.

There is no second sensor. A `claude agents --json` corroborator lived here and
was removed once nothing called it: an uncalled slow path is not corroboration,
it is a second parser of the same records drifting on its own.

Measured against `claude` 2.1.220, because the design spec describes this
wrongly in three ways:

  * keys are camelCase (`sessionId`, `startedAt`), not snake_case;
  * `startedAt` is epoch MILLISECONDS -- the only ms value in this codebase;
  * there is no `model` or `effort` field at all, so the live band takes those
    from our own tables.

And one the plan itself got wrong: **two record shapes share one array.**
`kind: "interactive"` carries `pid` + `status`; `kind: "background"` carries
`id` + `state` and has neither `pid` nor `status`. Defaulting a missing status
to "idle" therefore labels a *running* background agent idle, which is exactly
the false quiescence the design forbids. A record with neither field is
`unknown`.

"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from bridge.launcher import SESSION_ID_RE
from bridge.models import AgentsState, LiveSession

UNAVAILABLE = AgentsState(status="unavailable", sessions=[], source="none")

# Where the registry lives. One file per interactive session, named `<pid>.json`.
SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# Background agents are NOT in the registry directory; they live under the
# daemon's own roster. Named here so the omission is deliberate rather than an
# oversight the next reader has to rediscover.
DAEMON_ROSTER = Path.home() / ".claude" / "daemon" / "roster.json"

# Confirmed against `claude` 2.1.220. A background agent in one of these has
# stopped and is not occupying anything, so the live band must not show it as
# work in progress.
TERMINAL_STATES = frozenset({"done", "failed", "stopped"})

# What a record with neither `status` nor `state` gets. Deliberately not "idle":
# "we could not tell" and "it is sitting there doing nothing" are different
# claims, and only one of them is safe to make from a missing field.
UNKNOWN = "unknown"

# `procStart` and `ps -o lstart=` both truncate to whole seconds and are
# produced by different code paths, so an exact match is too strict.
PID_START_TOLERANCE_S = 2


def _ms_to_s(value) -> int:
    """Milliseconds to seconds. A missing `// 1000` lands in the year 58,000."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) // 1000


def normalize_status(entry: dict) -> str:
    """The one status field, from whichever key this record shape uses.

    Never defaults to `idle`, and never validates against a closed set: an
    unrecognised value round-trips verbatim so a new state renders as itself
    instead of being coerced into a wrong one.
    """
    for key in ("status", "state"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return UNKNOWN


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATES


def _parse_instant(text: str, assume_utc: bool) -> datetime | None:
    """Parse `Sat Aug  1 07:44:58 2026` into an aware datetime.

    The whole point of this function is the timezone argument. `procStart` in
    the registry is **UTC**; `ps -o lstart=` prints **local time**. On this
    machine they are the same instant five hours apart as strings, so a guard
    that string-compares them always fails -- the same class of bug as the
    ms-vs-seconds trap above. Both are parsed to instants and compared as such.
    """
    if not text:
        return None
    try:
        naive = datetime.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y")
    except (TypeError, ValueError):
        return None
    if assume_utc:
        return naive.replace(tzinfo=timezone.utc)
    return naive.astimezone()  # interprets a naive stamp as local time


def ps_start_times(pids, ps_run=subprocess.run) -> dict[int, datetime]:
    """Start times for many pids in ONE `ps` call.

    The registry read is only worth doing because it is ~3000x cheaper than the
    subprocess, and spawning one `ps` per live session would hand most of that
    back -- measured at 5.5 ms for a single session, against 0.1 ms for the file
    read itself. One call keeps the guard roughly free however many sessions
    are running.
    """
    pids = [p for p in pids if isinstance(p, int) and p > 0]
    if not pids:
        return {}
    try:
        proc = ps_run(
            ["/bin/ps", "-o", "pid=,lstart=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True, timeout=2.0,
        )
    except Exception:  # noqa: BLE001 - a failed cross-check must not hide cards
        return {}
    out: dict[int, datetime] = {}
    for line in (proc.stdout or "").splitlines():
        head, _, rest = line.strip().partition(" ")
        if not head.isdigit():
            continue
        when = _parse_instant(rest, assume_utc=False)
        if when is not None:
            out[int(head)] = when
    return out


def _start_matches(expected_utc: str | None, actual_local: datetime | None) -> bool:
    """True unless the two disagree. Both must be instants, never strings."""
    expected = _parse_instant(expected_utc or "", assume_utc=True)
    if expected is None or actual_local is None:
        # Unverifiable, so treat as alive: dropping a real running session is
        # the worse error of the two.
        return True
    return abs((actual_local - expected).total_seconds()) <= PID_START_TOLERANCE_S


def pid_exists(pid) -> bool:
    """`os.kill(pid, 0)` checks existence and sends no signal, so this does not
    breach "Bridge never supervises"."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, owned by somebody else
    except OSError:
        return False
    return True


def pid_is_alive(pid: int | None, proc_start: str | None, ps_run=subprocess.run) -> bool:
    """Whether `pid` is running AND is the process the registry file describes.

    `os.kill(pid, 0)` checks existence and sends no signal, so this does not
    breach "Bridge never supervises". The `procStart` cross-check defeats PID
    reuse: without it a recycled pid makes a dead session look live forever.
    An unverifiable start time is treated as alive-if-the-pid-exists rather
    than as dead, because dropping a real running session is the worse error.
    """
    if not pid_exists(pid):
        return False
    return _start_matches(proc_start, ps_start_times([pid], ps_run).get(pid))


def _session_from(entry: dict, kind_default: str = "interactive") -> LiveSession | None:
    """Project one record of either shape, or None if it cannot be correlated."""
    session_id = str(entry.get("sessionId") or entry.get("id") or "").lower()
    if not SESSION_ID_RE.match(session_id):
        return None  # no id, no correlation: drop the entry, keep the rest
    return LiveSession(
        session_id=session_id,
        cwd=str(entry.get("cwd") or ""),
        kind=str(entry.get("kind") or kind_default),
        status=normalize_status(entry),
        name=entry.get("name") or None,
        started_at=_ms_to_s(entry.get("startedAt")),
        pid=entry.get("pid") if isinstance(entry.get("pid"), int) else None,
        version=entry.get("version") or None,
        entrypoint=entry.get("entrypoint") or None,
        status_updated_at=_ms_to_s(entry.get("statusUpdatedAt")) or None,
        updated_at=_ms_to_s(entry.get("updatedAt")) or None,
        raw=dict(entry),
    )


def read_registry(sessions_dir=None, alive_fn=None) -> AgentsState:
    """The primary sensor: one JSON file per live interactive session.

    A missing directory is `unavailable`, not empty: "Claude has never run here"
    and "nothing is running" are different claims. One unreadable or malformed
    file is skipped rather than fatal -- these are written by another process
    and can be caught mid-write.
    """
    sessions_dir = Path(sessions_dir or SESSIONS_DIR)
    try:
        paths = sorted(sessions_dir.glob("*.json"))
    except OSError:
        return UNAVAILABLE
    if not sessions_dir.is_dir():
        return UNAVAILABLE

    candidates: list[tuple[LiveSession, dict]] = []
    for path in paths:
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # torn write or junk: skip the file, keep the rest
        if not isinstance(entry, dict):
            continue
        live = _session_from(entry)
        if live is not None:
            candidates.append((live, entry))

    # A registry file outlives its process: `claude` does not always clean up,
    # so an unguarded read reports long-dead sessions as running. Existence is
    # checked per pid (free, no subprocess) and the start times come from ONE
    # batched `ps` -- see `ps_start_times` for why that matters.
    if alive_fn is None:
        starts = ps_start_times([live.pid for live, _ in candidates])

        def alive_fn(pid, proc_start, _starts=starts):
            return pid_exists(pid) and _start_matches(proc_start, _starts.get(pid))

    sessions: list[LiveSession] = []
    version = None
    for live, entry in candidates:
        if not alive_fn(live.pid, entry.get("procStart")):
            continue
        version = version or live.version
        sessions.append(live)
    return AgentsState(status="ok", sessions=sessions, source="registry",
                       version=version)


def probe(sessions_dir=None) -> AgentsState:
    """The sensor the panel calls. Registry only: the subprocess is not a
    fallback, because a slow path on every SSE tick per connected tab is the
    cost this design exists to avoid."""
    return read_registry(sessions_dir)


# --- attribution -------------------------------------------------------------

# Sessions whose cwd matches no registered project. They are shown under this
# key rather than dropped: a dashboard whose headline feature is "what is
# running right now" must not lose rows.
UNATTRIBUTED = "\x00unattributed"


def by_project(
    state: AgentsState,
    alias_map: dict[str, str],
    registered_paths=(),
) -> dict[str, list[LiveSession]]:
    """Group live sessions by canonical project path.

    Resolution order is **exact match -> longest registered-path prefix ->
    the unattributed bucket**, and each step is load-bearing:

    * Exact before prefix, because `dev/projectY/nested-app` is registered
      *and* sits under registered `dev/projectY`; the more specific one is
      right.
    * Longest prefix, so a session started in a subdirectory of a project still
      lands on that project's card.
    * An explicit bucket rather than falling back to the cwd itself. Measured
      against the 30 registered projects: mapping cwd-through-aliases and
      defaulting to the cwd made `$HOME` itself match nothing and vanish
      from the dashboard entirely.
    """
    registered = sorted(registered_paths, key=len, reverse=True)
    grouped: dict[str, list[LiveSession]] = {}
    for session in state.sessions:
        cwd = alias_map.get(session.cwd, session.cwd)
        key = None
        if cwd in registered_paths:
            key = cwd
        else:
            for path in registered:  # longest first
                if cwd.startswith(path.rstrip("/") + "/"):
                    key = path
                    break
        grouped.setdefault(key if key is not None else UNATTRIBUTED, []).append(
            session
        )
    # Most recently started first, so a card showing one of several picks the
    # session the user most likely means.
    for sessions in grouped.values():
        sessions.sort(key=lambda s: -s.started_at)
    return grouped
