"""The liveness sensor.

Fixtures are the literal recorded payloads from `claude` 2.1.220 on this
machine, not hand-simplified ones -- a simplified fixture drifts from reality,
which is precisely how the design spec got this wrong three times.
"""

import json
import os
import subprocess
from datetime import datetime, timezone

import pytest

from bridge import agents
from bridge.models import AgentsState, LiveSession

SID_A = "20909ede-da8c-4f9f-928f-80fa779fbcc6"
SID_B = "4000ea8d-43a4-4074-8d6f-3adccdb98f04"
SID_BG = "eab23eb4-4734-4d73-99f9-f039bb891c51"

# Recorded verbatim from ~/.claude/sessions/53458.json.
REAL_REGISTRY = {
    "pid": 53458, "sessionId": SID_A, "cwd": "/Users/mitsheth/dev/bridge",
    "startedAt": 1785570300371, "procStart": "Sat Aug  1 07:44:58 2026",
    "version": "2.1.220", "peerProtocol": 1, "kind": "interactive",
    "entrypoint": "cli",
    "name": "Fixed the ~3x token overcount in transcripts.py (requestId d",
    "updatedAt": 1785570464356, "status": "busy",
    "statusUpdatedAt": 1785570464356,
}

# Recorded verbatim from `claude agents --json`.
REAL_PAYLOAD = json.dumps([
    {"pid": 10210, "cwd": "/Users/mitsheth/dev/projectY", "kind": "interactive",
     "startedAt": 1785536395229, "sessionId": SID_BG,
     "name": "projecty-80", "status": "idle"},
    {"pid": 19145, "cwd": "/Users/mitsheth/dev/bridge", "kind": "interactive",
     "startedAt": 1785548714710, "sessionId": SID_B,
     "name": "Built Bridge Phase 3", "status": "busy"},
])


def fake_run(code: int, out: str):
    def run(*a, **k):
        return subprocess.CompletedProcess(a[0] if a else [], code, out, "")
    return run


def always_alive(pid, proc_start, **kw):
    return True


def write_registry(tmp_path, *entries):
    d = tmp_path / "sessions"
    d.mkdir(exist_ok=True)
    for entry in entries:
        (d / f"{entry.get('pid', 'x')}.json").write_text(json.dumps(entry))
    return d


# --- the registry sensor -----------------------------------------------------


def test_the_registry_reads_camelcase_keys_and_converts_milliseconds(tmp_path):
    d = write_registry(tmp_path, REAL_REGISTRY)
    state = agents.read_registry(d, alive_fn=always_alive)

    assert state.status == "ok"
    assert state.source == "registry"
    s = state.sessions[0]
    assert s.session_id == SID_A
    assert s.status == "busy"
    assert s.kind == "interactive"
    # 1785570300371 ms -> 1785570300 s. A missing //1000 lands in year 58,000.
    assert s.started_at == 1785570300
    assert s.status_updated_at == 1785570464


def test_the_registry_carries_the_five_fields_the_subprocess_strips(tmp_path):
    """These are the whole reason the registry is the primary sensor."""
    d = write_registry(tmp_path, REAL_REGISTRY)
    s = agents.read_registry(d, alive_fn=always_alive).sessions[0]

    assert s.version == "2.1.220"
    assert s.entrypoint == "cli"
    assert s.status_updated_at is not None
    assert s.updated_at is not None
    assert s.raw["procStart"] == "Sat Aug  1 07:44:58 2026"


def test_a_missing_registry_directory_is_unavailable_not_empty(tmp_path):
    """"Claude has never run here" and "nothing is running" are different
    claims, and only one of them is safe to make."""
    state = agents.read_registry(tmp_path / "nope", alive_fn=always_alive)
    assert state.status == "unavailable"
    assert state.sessions == []


def test_an_empty_registry_directory_is_ok_with_no_sessions(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    state = agents.read_registry(d, alive_fn=always_alive)
    assert state.status == "ok"
    assert state.sessions == []


@pytest.mark.parametrize("junk", ["not json", "", "null", "[]", '"a string"'])
def test_one_malformed_registry_file_is_skipped_not_fatal(tmp_path, junk):
    """These files are written by another process and can be caught mid-write."""
    d = write_registry(tmp_path, REAL_REGISTRY)
    (d / "99999.json").write_text(junk)
    state = agents.read_registry(d, alive_fn=always_alive)
    assert state.status == "ok"
    assert [s.session_id for s in state.sessions] == [SID_A]


def test_a_registry_entry_with_no_session_id_is_skipped(tmp_path):
    d = write_registry(tmp_path, REAL_REGISTRY, {"pid": 7, "cwd": "/p"})
    state = agents.read_registry(d, alive_fn=always_alive)
    assert len(state.sessions) == 1


def test_a_registry_file_whose_process_is_gone_is_not_reported_as_running(tmp_path):
    """`claude` does not always clean up, so an unguarded read reports
    long-dead sessions as live forever."""
    d = write_registry(tmp_path, REAL_REGISTRY)
    state = agents.read_registry(d, alive_fn=lambda pid, ps, **k: False)
    assert state.status == "ok"
    assert state.sessions == []


# --- the two record shapes ---------------------------------------------------


def test_a_background_record_reads_state_not_status():
    """`kind: background` carries `state`, and has no `pid` and no `status`."""
    entry = {"id": SID_BG, "kind": "background", "state": "working",
             "cwd": "/p", "startedAt": 1785536395229}
    assert agents.normalize_status(entry) == "working"


def test_a_running_background_agent_is_never_labelled_idle():
    """The plan's `entry.get("status") or "idle"` labels this one idle, which is
    the exact false quiescence the design forbids."""
    entry = {"id": SID_BG, "kind": "background", "state": "working"}
    assert agents.normalize_status(entry) != "idle"


def test_a_record_with_neither_field_is_unknown_never_idle():
    """"We could not tell" and "it is sitting there doing nothing" are
    different claims."""
    assert agents.normalize_status({"id": SID_BG, "kind": "background"}) == "unknown"
    assert agents.normalize_status({}) == "unknown"


def test_an_unrecognised_state_round_trips_verbatim():
    """`status` is an open vocabulary. Coercing a new value into a known one
    would report a state the sensor never saw."""
    assert agents.normalize_status({"state": "rehydrating"}) == "rehydrating"
    assert agents.normalize_status({"status": "compacting"}) == "compacting"


def test_the_terminal_states_are_the_ones_extracted_from_the_binary():
    assert agents.TERMINAL_STATES == frozenset({"done", "failed", "stopped"})
    assert agents.is_terminal("done") and agents.is_terminal("failed")
    assert not agents.is_terminal("working")
    assert not agents.is_terminal("blocked")


def test_a_background_record_is_correlated_by_its_id_field(tmp_path):
    """Background records have `id` where interactive ones have `sessionId`.
    Reading only `sessionId` drops every background agent silently."""
    d = write_registry(tmp_path, {"pid": 1, "id": SID_BG, "kind": "background",
                                  "state": "working", "cwd": "/p"})
    state = agents.read_registry(d, alive_fn=always_alive)
    assert [s.session_id for s in state.sessions] == [SID_BG]
    assert state.sessions[0].status == "working"


# --- the PID-reuse guard -----------------------------------------------------


def test_proc_start_is_parsed_as_utc_and_ps_as_local_time():
    """The measured trap: the registry says 07:44:58 and `ps` says 02:44:58 for
    the SAME process. A guard that string-compares them always fails."""
    utc = agents._parse_instant("Sat Aug  1 07:44:58 2026", assume_utc=True)
    local = agents._parse_instant("Sat Aug  1 02:44:58 2026", assume_utc=False)
    assert utc is not None and local is not None
    # Same instant on a machine at UTC-5. Asserted as a difference so the test
    # does not itself depend on the runner's timezone.
    offset = local.utcoffset().total_seconds()
    assert (local - utc).total_seconds() == pytest.approx(-offset - 5 * 3600, abs=1)


def test_a_reused_pid_whose_start_time_disagrees_is_not_alive():
    pid = os.getpid()

    def ps(*a, **k):
        # `ps -o pid=,lstart=` emits the pid first; the batched reader keys on it.
        return subprocess.CompletedProcess(
            a[0], 0, f"{pid} Sat Aug  1 03:00:00 2026\n", ""
        )

    assert agents.pid_is_alive(pid, "Sat Jan  3 07:44:58 2026", ps_run=ps) is False


def test_a_matching_start_time_is_alive():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fmt = "%a %b %e %H:%M:%S %Y"
    local = now.astimezone()

    pid = os.getpid()

    def ps(*a, **k):
        return subprocess.CompletedProcess(
            a[0], 0, f"{pid} {local.strftime(fmt)}", ""
        )

    assert agents.pid_is_alive(pid, now.strftime(fmt), ps_run=ps) is True


def test_a_pid_that_does_not_exist_is_not_alive():
    assert agents.pid_is_alive(9999999, "Sat Aug  1 07:44:58 2026") is False


def test_a_failed_ps_leaves_the_session_alive_rather_than_dropping_it():
    """Dropping a real running session is the worse error of the two."""
    def boom(*a, **k):
        raise OSError("ps exploded")

    assert agents.pid_is_alive(os.getpid(), "Sat Aug  1 07:44:58 2026",
                               ps_run=boom) is True


def test_a_missing_proc_start_leaves_the_session_alive():
    assert agents.pid_is_alive(os.getpid(), None) is True


@pytest.mark.parametrize("pid", [None, 0, -1, "53458"])
def test_a_nonsense_pid_is_not_alive(pid):
    assert agents.pid_is_alive(pid, "Sat Aug  1 07:44:58 2026") is False


# --- the subprocess corroborator ---------------------------------------------


def test_the_subprocess_reads_the_real_recorded_payload():
    state = agents.probe_subprocess(claude="/bin/claude", run=fake_run(0, REAL_PAYLOAD))
    assert state.status == "ok"
    assert state.source == "subprocess"
    live = {s.session_id: s for s in state.sessions}
    assert live[SID_B].status == "busy"
    assert live[SID_B].started_at == 1785548714


def test_a_nonzero_exit_is_unavailable_not_empty():
    """The payload is deliberately VALID JSON.

    With empty stdout the JSON parse fails anyway, so the test would pass with
    no returncode check at all -- and a `claude` that exits nonzero while still
    printing a plausible list would be believed.
    """
    state = agents.probe_subprocess(claude="/bin/claude", run=fake_run(1, REAL_PAYLOAD))
    assert state.status == "unavailable"
    assert state.sessions == []


@pytest.mark.parametrize("payload", ["not json", "", "null", "[[]]", "3"])
def test_malformed_subprocess_output_is_unavailable(payload):
    assert agents.probe_subprocess(
        claude="/bin/claude", run=fake_run(0, payload)
    ).status == "unavailable"


def test_a_subprocess_timeout_is_unavailable():
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=3.0)

    assert agents.probe_subprocess(claude="/bin/claude", run=boom).status == "unavailable"


def test_an_entry_missing_its_session_id_is_skipped_not_fatal():
    payload = json.dumps([{"cwd": "/p"}, json.loads(REAL_PAYLOAD)[1]])
    state = agents.probe_subprocess(claude="/bin/claude", run=fake_run(0, payload))
    assert state.status == "ok"
    assert len(state.sessions) == 1


def test_the_subprocess_reads_sessionId_not_session_id():
    """Reading `session_id` returns zero sessions against real output, which is
    the exact bug the design spec would have caused."""
    state = agents.probe_subprocess(claude="/bin/claude", run=fake_run(0, REAL_PAYLOAD))
    assert len(state.sessions) == 2


# --- attribution -------------------------------------------------------------

REGISTERED = [
    "/Users/mitsheth/dev/projectY",
    "/Users/mitsheth/dev/projectY/boardwatch",
    "/Users/mitsheth/dev/bridge",
]


def live(cwd, sid=SID_A, started=1):
    return LiveSession(session_id=sid, cwd=cwd, kind="interactive",
                       status="busy", started_at=started)


def group(*sessions, aliases=None):
    return agents.by_project(
        AgentsState(status="ok", sessions=list(sessions)),
        aliases or {}, REGISTERED,
    )


def test_an_exact_cwd_match_wins_over_a_shorter_registered_prefix():
    """`boardwatch` is registered AND sits under registered `projectY`. The
    more specific one is right; prefix-first would put it on the wrong card."""
    grouped = group(live("/Users/mitsheth/dev/projectY/boardwatch"))
    assert list(grouped) == ["/Users/mitsheth/dev/projectY/boardwatch"]


def test_a_subdirectory_falls_back_to_its_longest_registered_prefix():
    grouped = group(live("/Users/mitsheth/dev/bridge/src/bridge"))
    assert list(grouped) == ["/Users/mitsheth/dev/bridge"]


def test_the_longest_prefix_wins_not_the_first_one_found():
    grouped = group(live("/Users/mitsheth/dev/projectY/boardwatch/deep/inside"))
    assert list(grouped) == ["/Users/mitsheth/dev/projectY/boardwatch"]


def test_an_unregistered_cwd_lands_in_the_unattributed_bucket_not_nowhere():
    """Measured: `/Users/mitsheth` matched none of the 30 registered projects
    and vanished from the dashboard entirely."""
    grouped = group(live("/Users/mitsheth"))
    assert list(grouped) == [agents.UNATTRIBUTED]
    assert len(grouped[agents.UNATTRIBUTED]) == 1


def test_no_live_session_is_ever_dropped():
    grouped = group(
        live("/Users/mitsheth/dev/bridge", sid=SID_A),
        live("/Users/mitsheth", sid=SID_B),
        live("/tmp/somewhere", sid=SID_BG),
    )
    assert sum(len(v) for v in grouped.values()) == 3


def test_an_aliased_cwd_maps_to_its_canonical_project():
    grouped = group(
        live("/Users/mitsheth/Documents/projectY"),
        aliases={"/Users/mitsheth/Documents/projectY": "/Users/mitsheth/dev/projectY"},
    )
    assert list(grouped) == ["/Users/mitsheth/dev/projectY"]


def test_a_sibling_sharing_a_name_prefix_is_not_matched():
    """`/dev/bridge-old` starts with `/dev/bridge` as a STRING but is a
    different project. Matching on the raw prefix would merge them."""
    grouped = group(live("/Users/mitsheth/dev/bridge-old"))
    assert list(grouped) == [agents.UNATTRIBUTED]


def test_several_sessions_on_one_project_are_most_recent_first():
    grouped = group(
        live("/Users/mitsheth/dev/bridge", sid=SID_A, started=100),
        live("/Users/mitsheth/dev/bridge", sid=SID_B, started=900),
    )
    assert [s.session_id for s in grouped["/Users/mitsheth/dev/bridge"]] == [SID_B, SID_A]


def test_the_pid_guard_makes_exactly_one_ps_call_however_many_sessions(tmp_path):
    """The registry sensor is only worth having because it is cheap. One `ps`
    per session would hand most of that back on every SSE tick."""
    entries = [dict(REAL_REGISTRY, pid=1000 + n,
                    sessionId=f"{n:08d}-0000-0000-0000-000000000000")
               for n in range(12)]
    d = write_registry(tmp_path, *entries)
    calls = []

    def counting_ps(*a, **k):
        calls.append(a[0])
        return subprocess.CompletedProcess(a[0], 0, "", "")

    original = agents.ps_start_times
    try:
        agents.ps_start_times = lambda pids, ps_run=None: original(pids, counting_ps)
        agents.read_registry(d)
    finally:
        agents.ps_start_times = original

    assert len(calls) == 1, f"{len(calls)} ps calls for 12 sessions"
    # And every pid went into that single call.
    assert calls[0].count("-p") == 1
    assert "1011" in calls[0][-1]
