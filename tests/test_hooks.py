"""Hook events: the only route to a `needs_input` state.

No JSONL entry type records a permission prompt, so nothing else in Bridge can
learn that a session is sitting at a prompt waiting for a human.
"""

import pytest
from fastapi.testclient import TestClient

from bridge import agents, hooks
from bridge.api import create_app
from bridge.cards import build_cards, live_priority
from bridge.config import load
from bridge.models import AgentsState, GitState, LiveSession, SessionRecord
from bridge.store import Store

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def notification(kind, session_id=SID):
    return {"session_id": session_id, "hook_event_name": "Notification",
            "notification_type": kind, "cwd": "/p/one"}


# --- the state machine -------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(hooks.NEEDS_INPUT_TYPES))
def test_every_waiting_notification_marks_the_session(kind):
    state = hooks.HookState()
    state.record(notification(kind), now=0)
    assert state.is_waiting(SID, now=1)


def test_agent_completed_clears_the_wait():
    """The prompt, if there was one, is no longer outstanding."""
    state = hooks.HookState()
    state.record(notification("permission_prompt"), now=0)
    state.record(notification("agent_completed"), now=1)
    assert state.is_waiting(SID, now=2) is False


def test_an_unrecognised_notification_type_clears_rather_than_sets():
    """Guessing that an unknown notification means "waiting" would leave cards
    claiming attention nobody asked for."""
    state = hooks.HookState()
    state.record(notification("permission_prompt"), now=0)
    state.record(notification("something_new"), now=1)
    assert state.is_waiting(SID, now=2) is False


@pytest.mark.parametrize("event_name", ["SessionStart", "SessionEnd"])
def test_session_start_and_end_both_clear_the_wait(event_name):
    """A session that just started is not waiting, and one that ended cannot be."""
    state = hooks.HookState()
    state.record(notification("permission_prompt"), now=0)
    state.record({"session_id": SID, "hook_event_name": event_name}, now=1)
    assert state.is_waiting(SID, now=2) is False


def test_a_wait_expires_so_a_missed_clear_does_not_last_forever():
    """Hook events are silently lost whenever Bridge is down. Without a TTL a
    card would claim "waiting for you" until the process restarted."""
    state = hooks.HookState(ttl_s=10)
    state.record(notification("permission_prompt"), now=0)
    assert state.is_waiting(SID, now=9) is True
    assert state.is_waiting(SID, now=11) is False


def test_one_session_waiting_does_not_mark_another():
    state = hooks.HookState()
    state.record(notification("permission_prompt", session_id=SID), now=0)
    assert state.is_waiting(OTHER, now=1) is False


@pytest.mark.parametrize("event", [
    None, "a string", 42, {}, {"hook_event_name": "Notification"},
    {"session_id": "", "hook_event_name": "Notification"},
    {"session_id": 7, "hook_event_name": "Notification"},
])
def test_a_malformed_event_is_ignored_not_fatal(event):
    """A hook that raises is noise in somebody's unrelated session."""
    assert hooks.HookState().record(event, now=0) is None


def test_forget_reconciles_against_the_liveness_sensor():
    """The sensor decides what exists. A session it no longer reports cannot be
    waiting, whatever the last hook said."""
    state = hooks.HookState()
    state.record(notification("permission_prompt", session_id=SID), now=0)
    state.record(notification("permission_prompt", session_id=OTHER), now=0)
    state.forget([SID])
    assert state.waiting_ids(now=1) == {SID}


# --- the route ---------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
    cfg = load({"db_path": tmp_path / "h.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    application = create_app(store, cfg)
    yield TestClient(application), store, application
    store.close()


def test_the_route_records_a_permission_prompt(app):
    c, _, application = app
    assert c.post("/api/hooks", json=notification("permission_prompt")).status_code == 200
    assert application.state.hook_state.is_waiting(SID)


@pytest.mark.parametrize("body", [b"not json", b"", b"[1,2,3]", b"null"])
def test_a_malformed_body_still_answers_200(app, body):
    """A hook that errors or hangs affects every Claude session on the machine,
    not just Bridge's. This route must always answer, always fast."""
    c, _, _ = app
    r = c.post("/api/hooks", content=body,
               headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_an_event_for_an_unknown_session_is_harmless(app):
    c, _, _ = app
    assert c.post("/api/hooks", json={"session_id": "nope",
                                      "hook_event_name": "SessionEnd"}
                  ).status_code == 200


# --- the overlay on the card -------------------------------------------------


def test_a_waiting_session_shows_needs_input_on_its_card(tmp_path):
    cfg = load({"db_path": tmp_path / "o.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/p/one", "one")
    store.upsert_session(
        SessionRecord(session_id=SID, transcript_path="/t/x",
                      ended_at="2026-07-30T10:00:00.000Z"), pid)
    state = hooks.HookState()
    # No injected `now`: build_cards reads the real monotonic clock, and an
    # entry stamped 0 is already 600 s stale by the TTL's reckoning.
    state.record(notification("permission_prompt"))

    def live():
        return AgentsState(status="ok", sessions=[LiveSession(
            session_id=SID, cwd="/p/one", kind="interactive", status="idle")])

    card = build_cards(store, cfg, probe_fn=lambda p: GitState(status="ok"),
                       agents_fn=live, hook_state=state)[0]
    store.close()
    assert card.live.status == hooks.NEEDS_INPUT


def test_the_overlay_cannot_invent_a_session_the_sensor_cannot_see(tmp_path):
    """Hooks are an overlay, never a substitute. A hook for a session the
    sensor does not report must not conjure a live band."""
    cfg = load({"db_path": tmp_path / "o2.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    store.upsert_project("/p/one", "one")
    state = hooks.HookState()
    state.record(notification("permission_prompt"))

    card = build_cards(store, cfg, probe_fn=lambda p: GitState(status="ok"),
                       agents_fn=lambda: AgentsState(status="ok", sessions=[]),
                       hook_state=state)[0]
    store.close()
    assert card.live is None


def test_needs_input_sits_at_the_top_of_the_attention_ladder():
    assert live_priority(hooks.NEEDS_INPUT) == 0
    assert live_priority(hooks.NEEDS_INPUT) < live_priority("busy")
    assert live_priority(hooks.NEEDS_INPUT) < live_priority("idle")


def test_a_failed_sensor_does_not_apply_the_overlay(tmp_path):
    """With no sensor reading there is nothing to overlay onto, and asserting
    "waiting" from a failed probe is the same false claim as asserting idle."""
    cfg = load({"db_path": tmp_path / "o3.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    store.upsert_project("/p/one", "one")
    state = hooks.HookState()
    state.record(notification("permission_prompt"))

    card = build_cards(store, cfg, probe_fn=lambda p: GitState(status="ok"),
                       agents_fn=lambda: AgentsState(status="unavailable"),
                       hook_state=state)[0]
    store.close()
    assert card.live is None
    assert card.live_unavailable is True
    # And the hook state was NOT reconciled away by a failed read.
    assert state.is_waiting(SID) is True


def test_a_bug_inside_record_cannot_escape_into_the_posting_session(tmp_path,
                                                                    monkeypatch):
    """The malformed-body cases never reach `record`'s body, so they cannot
    exercise this guard. A Bridge bug must surface as a Bridge bug, not as a
    failing hook inside whatever session happened to fire it.
    """
    def boom(self, event, now=None):
        raise RuntimeError("hook state exploded")

    monkeypatch.setattr(hooks.HookState, "record", boom)
    cfg = load({"db_path": tmp_path / "b.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    c = TestClient(create_app(store, cfg))
    r = c.post("/api/hooks", json=notification("permission_prompt"))
    store.close()

    assert r.status_code == 200
    assert r.json() == {"ok": True}
