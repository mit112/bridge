import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bridge import launcher, spool
from bridge.api import create_app
from bridge.config import load
from bridge.models import Handoff, Launch, SessionRecord
from bridge.registry import resolve_project
from bridge.store import Store

DEMO = "/Users/mitsheth/dev/demo"


@pytest.fixture
def client(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo",
                      title="Did the work", ended_at="2026-07-30T10:00:00.000Z",
                      model="claude-opus-5", effort="high", tokens_in=5,
                      tokens_out=5),
        pid,
    )
    app = create_app(store, cfg)
    yield TestClient(app), store, pid
    store.close()


def test_projects_endpoint_lists_projects(client):
    c, _, _ = client
    r = c.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["name"] == "demo"


def test_dashboard_renders_project_and_title(client):
    c, _, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "demo" in r.text
    assert "Did the work" in r.text


def test_dashboard_renders_with_zero_projects(tmp_path):
    cfg = load({"db_path": tmp_path / "empty.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    c = TestClient(create_app(store, cfg))
    r = c.get("/")
    assert r.status_code == 200
    store.close()


def test_project_detail_renders(client):
    c, _, pid = client
    r = c.get(f"/project/{pid}")
    assert r.status_code == 200
    assert "Did the work" in r.text


def test_unknown_project_returns_404(client):
    c, _, _ = client
    assert c.get("/project/99999").status_code == 404


def test_refresh_returns_stats(client):
    c, _, _ = client
    r = c.post("/api/refresh")
    assert r.status_code == 200
    assert "files_seen" in r.json()


def test_stale_project_shows_warning_glyph_and_text(tmp_path):
    """Status must not be conveyed by color alone (WCAG 2.2 AA)."""
    from bridge.models import GitState

    import bridge.cards as cards_mod

    cfg = load({"db_path": tmp_path / "s.db", "spool_dir": tmp_path / "spool", "stale_hours": 1})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/stalerepo", "stalerepo")
    store.upsert_session(
        SessionRecord(session_id="s9", transcript_path="/t/s9",
                      project_path="/Users/mitsheth/dev/stalerepo", title="Old work",
                      ended_at="2026-07-30T10:00:00.000Z"),
        pid,
    )
    orig = cards_mod.gitprobe.probe
    cards_mod.gitprobe.probe = lambda p: GitState(
        status="ok", branch="main", dirty_count=47, oldest_uncommitted_at=1
    )
    try:
        c = TestClient(create_app(store, cfg))
        text = c.get("/").text
        assert "47 dirty" in text
        assert "⚠" in text
        assert "uncommitted" in text.lower()
    finally:
        cards_mod.gitprobe.probe = orig
        store.close()


def test_not_a_repo_shows_neutral_note_not_warning(tmp_path):
    from bridge.models import GitState

    import bridge.cards as cards_mod

    cfg = load({"db_path": tmp_path / "n.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/plain", "plain")
    store.upsert_session(
        SessionRecord(session_id="s10", transcript_path="/t/s10",
                      project_path="/Users/mitsheth/dev/plain", title="Work",
                      ended_at="2026-07-30T10:00:00.000Z"),
        pid,
    )
    orig = cards_mod.gitprobe.probe
    cards_mod.gitprobe.probe = lambda p: GitState(status="not_a_repo")
    try:
        text = TestClient(create_app(store, cfg)).get("/").text
        assert "not a git repo" in text.lower()
        assert "⚠" not in text
    finally:
        cards_mod.gitprobe.probe = orig
        store.close()


def test_tokens_shown_as_absolute_not_percentage(client):
    c, _, _ = client
    text = c.get("/").text
    assert "% of" not in text  # no fabricated denominator
    assert "today" in text.lower()


def test_concurrent_requests_do_not_error(tmp_path):
    """FastAPI dispatches sync routes to a threadpool sharing one connection.

    Without a lock this fails almost every request with
    sqlite3.InterfaceError / 'NoneType' is not subscriptable.
    """
    import threading

    cfg = load({"db_path": tmp_path / "conc.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    for n in range(12):
        pid = store.upsert_project(f"/p/{n}", f"p{n}")
        store.upsert_session(
            SessionRecord(session_id=f"s{n}", transcript_path=f"/t/{n}",
                          project_path=f"/p/{n}", title=f"t{n}",
                          ended_at="2026-07-30T10:00:00.000Z",
                          tokens_in=5, tokens_out=5),
            pid,
        )
    client = TestClient(create_app(store, cfg))
    codes: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(16)

    def hit():
        try:
            barrier.wait()
            for _ in range(6):
                codes.append(client.get("/").status_code)
        except Exception as e:   # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=hit) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store.close()
    assert errors == [], errors[:3]
    assert codes and all(c == 200 for c in codes), sorted(set(codes))


def test_concurrent_mixed_routes_do_not_error(tmp_path):
    """Smoke test, NOT a gate: measured detection is 10/20.

    GET /project/{id} once used store.conn directly, bypassing the lock, which
    both crashed and returned 404 for rows that exist. Reintroducing that bug
    fails this test only about half the time — the interleaving is timing
    dependent and cannot be forced from Python threads. It never fails on
    correct code (0/5), so it is kept as a cheap smoke check; the deterministic
    guard for this bug class is
    test_no_module_outside_store_touches_the_raw_connection.
    """
    import threading

    cfg = load({"db_path": tmp_path / "mixed.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pids = []
    for n in range(10):
        pid = store.upsert_project(f"/p/{n}", f"p{n}")
        pids.append(pid)
        store.upsert_session(
            SessionRecord(session_id=f"s{n}", transcript_path=f"/t/{n}",
                          project_path=f"/p/{n}", title=f"t{n}",
                          ended_at="2026-07-30T10:00:00.000Z",
                          tokens_in=5, tokens_out=5),
            pid,
        )
    client = TestClient(create_app(store, cfg))
    codes: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(16)

    def hit(i: int):
        try:
            barrier.wait()
            for n in range(6):
                codes.append(client.get("/").status_code)
                codes.append(client.get(f"/project/{pids[(i + n) % len(pids)]}").status_code)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store.close()
    assert errors == [], errors[:3]
    # A 404 here would mean an interleaved cursor lost a row that exists.
    assert codes and all(c == 200 for c in codes), sorted(set(codes))


def test_no_module_outside_store_touches_the_raw_connection():
    """The lock lives inside Store's methods, so any `.conn` use elsewhere
    bypasses it. The concurrency tests below catch that race only about half
    the time; this catches it every time, and it is what actually failed when
    the first fix left three call sites in api.py and indexer.py unconverted.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "bridge"
    offenders = {
        f.name: [
            f"{n}: {line.strip()}"
            for n, line in enumerate(f.read_text().splitlines(), 1)
            if ".conn" in line
        ]
        for f in sorted(src.glob("*.py"))
        if f.name != "store.py"
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert offenders == {}, f"raw connection access outside store.py: {offenders}"


# --- Phase 2: handoff routes -------------------------------------------------


HOSTILE_PROMPT = (
    'quotes " and \' and backticks `whoami`\n'
    "shell substitution $(echo pwned) and ${HOME}\n"
    "a windows path C:\\Users\\x and a tab\there\n"
    "unicode: émoji 🌉 and markup <script>alert(1)</script>\n"
)


@pytest.fixture
def handoff_app(tmp_path):
    cfg = load({"db_path": tmp_path / "h.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    yield TestClient(create_app(store, cfg)), store, cfg
    store.close()


def body(hid="h1", path=DEMO, prompt="carry on from here", **kw):
    b = dict(
        id=hid, project_path=path, next_prompt=prompt, session_id="sess-1",
        summary="a summary", suggested_model="claude-opus-5",
        suggested_effort="high",
    )
    b.update(kw)
    return b


def test_post_from_an_aliased_path_attaches_to_the_canonical_project(handoff_app):
    """A handoff from an old ~/Documents cwd must not re-split merged history."""
    c, store, _ = handoff_app
    store.set_alias("/Users/mitsheth/Documents/projectX", "/Users/mitsheth/dev/projectX")

    r = c.post("/api/handoff", json=body(path="/Users/mitsheth/Documents/projectX"))

    assert r.status_code == 201
    assert store.project_by_path("/Users/mitsheth/Documents/projectX") is None
    canonical = store.project_by_path("/Users/mitsheth/dev/projectX")
    assert canonical is not None
    assert r.json()["project_id"] == canonical["id"]
    assert c.get(f"/api/handoff/{canonical['id']}").json()["id"] == "h1"


def test_post_from_a_path_with_no_project_row_creates_one(handoff_app):
    """Capturing a handoff must never 404 because the project is unindexed."""
    c, store, _ = handoff_app
    r = c.post("/api/handoff", json=body(path="/Users/mitsheth/dev/brand-new"))
    assert r.status_code == 201
    row = store.project_by_path("/Users/mitsheth/dev/brand-new")
    assert row is not None
    assert row["name"] == "brand-new"


def test_posting_the_same_id_twice_yields_one_row(handoff_app):
    """A spool drain and a live POST of the same handoff cannot both insert."""
    c, store, _ = handoff_app
    first = c.post("/api/handoff", json=body("dup"))
    second = c.post("/api/handoff", json=body("dup", prompt="a different prompt"))

    assert first.status_code == 201
    assert second.status_code == 201
    pid = first.json()["project_id"]
    assert len(store.handoffs(pid)) == 1
    assert store.queued_handoff(pid)["next_prompt"] == "carry on from here"


def test_a_hostile_prompt_round_trips_byte_for_byte(handoff_app):
    """Prompts contain quotes, backticks, newlines and `$(...)`, and are large."""
    c, _, _ = handoff_app
    prompt = HOSTILE_PROMPT + "padding " * 5000
    assert len(prompt) > 40_000

    pid = c.post("/api/handoff", json=body(prompt=prompt)).json()["project_id"]
    got = c.get(f"/api/handoff/{pid}").json()["next_prompt"]

    assert got == prompt
    assert len(got) == len(prompt)


def test_boot_drain_ingests_a_spooled_handoff_before_serving(tmp_path):
    cfg = load({"db_path": tmp_path / "b.db", "spool_dir": tmp_path / "spool"})
    spool.write(
        Handoff(id="spooled", project_path=DEMO, next_prompt="from the spool",
                created_at=5),
        cfg.spool_dir,
    )
    store = Store(cfg.db_path)

    c = TestClient(create_app(store, cfg))

    pid = store.project_by_path(DEMO)["id"]
    assert c.get(f"/api/handoff/{pid}").json()["id"] == "spooled"
    assert spool.pending_count(cfg.spool_dir) == 0
    store.close()


def test_an_unreadable_spool_does_not_stop_the_panel_from_starting(tmp_path, monkeypatch):
    """A session must never lose the panel because the spool is broken."""
    cfg = load({"db_path": tmp_path / "u.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)

    def unreadable(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(spool, "drain", unreadable)
    app = create_app(store, cfg)

    assert TestClient(app).get("/").status_code == 200
    assert "error" in app.state.boot_drain
    store.close()


def test_get_returns_204_when_nothing_is_queued(handoff_app):
    c, store, _ = handoff_app
    pid = store.upsert_project(DEMO, "demo")
    r = c.get(f"/api/handoff/{pid}")
    assert r.status_code == 204
    assert r.content == b""


def test_patch_sets_status_and_rejects_unknown_ids_and_statuses(handoff_app):
    c, _, _ = handoff_app
    pid = c.post("/api/handoff", json=body("h1")).json()["project_id"]

    r = c.patch("/api/handoff/h1", json={"status": "consumed"})
    assert r.status_code == 200
    assert r.json()["status"] == "consumed"
    assert c.get(f"/api/handoff/{pid}").status_code == 204

    assert c.patch("/api/handoff/nope", json={"status": "consumed"}).status_code == 404
    assert c.patch("/api/handoff/h1", json={"status": "banana"}).status_code == 422


def test_a_live_post_is_journaled_so_the_database_stays_disposable(tmp_path):
    """A live POST never passes through the outbox.

    If the server did not journal it, the journal would only ever hold handoffs
    captured while the panel was *down*, and `rm ~/.bridge/bridge.db` would lose
    every one captured while it was up. That is the invariant this phase is
    supposed to preserve, so it is asserted end to end.
    """
    cfg = load({"db_path": tmp_path / "j.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    c = TestClient(create_app(store, cfg))

    r = c.post("/api/handoff", json=body("live", prompt="captured while up"))
    assert r.json()["journaled"] is True
    assert (cfg.spool_dir / "drained" / "live.json").exists()
    # It goes straight to the journal, not the outbox: nothing is left pending.
    assert spool.pending_count(cfg.spool_dir) == 0
    store.close()

    cfg.db_path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)

    store2 = Store(cfg.db_path)
    assert store2.handoff_count() == 0
    assert spool.rebuild_if_empty(store2, cfg.spool_dir).drained == 1
    pid = store2.project_by_path(DEMO)["id"]
    assert store2.queued_handoff(pid)["next_prompt"] == "captured while up"
    store2.close()


def test_a_queued_prompt_is_html_escaped_on_the_card(handoff_app):
    """A prompt is arbitrary text and routinely contains markup."""
    c, _, _ = handoff_app
    prompt = "before <script>alert('xss')</script> after"
    c.post("/api/handoff", json=body("h1", prompt=prompt))

    html = c.get("/").text

    assert "<script>alert(" not in html, "the prompt was rendered as live markup"
    assert "&lt;script&gt;alert(" in html
    assert "Copy prompt" in html


def test_the_card_shows_the_handoff_and_a_labelled_copy_affordance(handoff_app):
    c, _, _ = handoff_app
    c.post("/api/handoff", json=body("h1", prompt="carry on from here"))

    html = c.get("/").text

    assert "Next step queued" in html
    assert "a summary" in html
    # The button says what it does, and the confirmation is a live region so it
    # is announced without moving focus.
    assert ">Copy prompt<" in html
    # Asserted on the copy span itself, not on the page: the card renders three
    # status lines, so a bare `role="status"` substring stays true after the
    # copy confirmation loses its live region entirely.
    copy_status = re.search(r'<span[^>]*data-copy-status="handoff-h1"[^>]*>', html)
    assert copy_status, "no copy-confirmation element on the card"
    assert 'role="status"' in copy_status.group(0)
    assert 'aria-live="polite"' in copy_status.group(0)
    assert 'data-copy-target="handoff-h1"' in html
    assert 'id="handoff-h1"' in html


def test_a_card_with_no_handoff_shows_no_empty_affordance(client):
    """No orphan Copy button, no empty block, on the majority of cards."""
    c, _, _ = client
    html = c.get("/").text
    assert "Next step queued" not in html
    assert "Copy prompt" not in html
    assert "data-copy-target" not in html


def test_the_project_page_lists_past_handoffs_with_their_status(handoff_app):
    c, store, _ = handoff_app
    pid = c.post("/api/handoff", json=body("old", prompt="first")).json()["project_id"]
    c.post("/api/handoff", json=body("new", prompt="second"))

    html = c.get(f"/project/{pid}").text

    assert "Handoffs, most recent first" in html
    assert "superseded" in html
    assert "queued" in html


# --- Phase 3: the launcher ---------------------------------------------------

SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def recording_launcher(result=None):
    """A `launch_fn` double that records its calls.

    Assign `.result` to steer it: a `LaunchResult` is returned, an exception is
    raised, which is the difference between a spawn that failed and a launch that
    was refused before anything was recorded.

    Every test below passes one of these. `create_app`'s default is the real
    `launcher.launch`, which shells out to `/usr/bin/osascript` and would open a
    real Terminal window running a real, token-burning session -- so this is not
    testability polish, it is what makes it impossible for a route test to spawn
    anything by accident.

    It reproduces the two side effects of a real launch that the routes are held
    to: `launch()` resolves the project through the alias table, and a *started*
    launch consumes its handoff. Task 3's tests prove the real launcher does both;
    without them here a route test could pass while asserting nothing.
    """
    calls: list[tuple] = []

    def launch_fn(store, cfg, spec, handoff_id=None, **kwargs):
        calls.append((spec, handoff_id))
        if isinstance(launch_fn.result, Exception):
            # A `LaunchError` refuses the launch before any row exists, so this
            # returns nothing and writes nothing, exactly as the real one does.
            raise launch_fn.result
        resolve_project(store, spec.project_path)
        if launch_fn.result.outcome == "started" and handoff_id:
            store.set_handoff_status(handoff_id, "consumed")
        return launch_fn.result

    launch_fn.calls = calls
    launch_fn.result = result or launcher.LaunchResult(
        launch_id="l1", outcome="started", session_id=SESSION_ID,
        short_id=SESSION_ID[:8],
    )
    return launch_fn


@pytest.fixture
def launch_app(tmp_path):
    """An app wired to a recording launch double.

    `launches_dir` is overridden alongside `spool_dir` even though nothing here
    spawns: `conftest`'s autouse guard raises `RealBridgeDirTouched` -- a
    `BaseException`, so no catch-all can swallow it -- the moment a launcher
    writer sees the real `~/.bridge`, and a fixture that forgot the override would
    litter the user's own launches directory.
    """
    cfg = load({
        "db_path": tmp_path / "l.db",
        "spool_dir": tmp_path / "spool",
        "launches_dir": tmp_path / "launches",
    })
    store = Store(cfg.db_path)
    fake = recording_launcher()
    yield TestClient(create_app(store, cfg, launch_fn=fake)), store, cfg, fake
    store.close()


def test_a_launch_from_an_aliased_path_attaches_to_the_canonical_project(launch_app):
    """A ▶ pressed on an old ~/Documents path must not re-split merged history.

    The launch sends no prompt, so the route has to resolve the alias itself to
    find the queued handoff at all -- which is the resolution being asserted.
    """
    c, store, _, fake = launch_app
    store.set_alias("/Users/mitsheth/Documents/projectX", "/Users/mitsheth/dev/projectX")
    c.post("/api/handoff", json=body("h1", path="/Users/mitsheth/dev/projectX"))

    r = c.post("/api/launch",
               json={"project_path": "/Users/mitsheth/Documents/projectX"})

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "started"
    assert r.json()["handoff_id"] == "h1"
    assert store.project_by_path("/Users/mitsheth/Documents/projectX") is None
    assert store.project_by_path("/Users/mitsheth/dev/projectX") is not None
    # The raw path is handed to the launcher unchanged; `launch()` canonicalises
    # it through the same alias table rather than the route doing it twice.
    spec, handoff_id = fake.calls[0]
    assert spec.project_path == "/Users/mitsheth/Documents/projectX"
    assert handoff_id == "h1"


def test_a_failed_launch_is_a_200_carrying_the_error_and_the_prompt(launch_app):
    """The panel needs both in one response to show one and copy the other."""
    c, _, _, fake = launch_app
    c.post("/api/handoff", json=body("h1", prompt="carry on from here"))
    fake.result = launcher.LaunchResult(
        launch_id="l9", outcome="failed", error="/usr/bin/osascript exited 1: boom"
    )

    r = c.post("/api/launch", json={"project_path": DEMO})

    assert r.status_code == 200, r.text
    got = r.json()
    assert got["outcome"] == "failed"
    assert got["error"], "a failed launch must say why"
    assert got["prompt"] == "carry on from here"  # the clipboard fallback
    assert got["session_id"] is None
    # And the prompt is still queued, so the user is never stuck.
    assert c.get("/api/handoff", params={"project_path": DEMO}).json()["id"] == "h1"


def test_patch_next_prompt_changes_the_queued_text_and_leaves_it_queued(launch_app):
    """An inline edit persists, and editing is not consuming."""
    c, _, cfg, _ = launch_app
    pid = c.post("/api/handoff",
                 json=body("h1", prompt="the old plan")).json()["project_id"]

    r = c.patch("/api/handoff/h1", json={"next_prompt": "the edited plan"})

    assert r.status_code == 200, r.text
    assert r.json()["next_prompt"] == "the edited plan"
    assert r.json()["status"] == "queued"
    got = c.get(f"/api/handoff/{pid}").json()
    assert got["next_prompt"] == "the edited plan"
    assert got["status"] == "queued"
    # Re-journalled, so `rm ~/.bridge/bridge.db` restores the edit and not the
    # text it replaced.
    journal = json.loads(
        (cfg.spool_dir / "drained" / "h1.json").read_text(encoding="utf-8")
    )
    assert journal["next_prompt"] == "the edited plan"


def test_a_hostile_edited_prompt_round_trips_byte_for_byte(launch_app):
    """The Phase 2 assertion for POST, applied to the edit path."""
    c, _, _, _ = launch_app
    prompt = HOSTILE_PROMPT + "padding " * 5000
    assert len(prompt) > 40_000
    pid = c.post("/api/handoff",
                 json=body("h1", prompt="short")).json()["project_id"]

    assert c.patch("/api/handoff/h1", json={"next_prompt": prompt}).status_code == 200

    got = c.get(f"/api/handoff/{pid}").json()["next_prompt"]
    assert got == prompt
    assert len(got) == len(prompt)


def test_a_patch_with_neither_field_is_422_not_a_silent_no_op(launch_app):
    """Two optional fields alone would make `PATCH {}` a 200 that did nothing."""
    c, store, _, _ = launch_app
    c.post("/api/handoff", json=body("h1", prompt="unchanged"))

    assert c.patch("/api/handoff/h1", json={}).status_code == 422
    assert c.patch("/api/handoff/h1",
                   json={"status": None, "next_prompt": None}).status_code == 422

    row = store.get_handoff("h1")
    assert row["next_prompt"] == "unchanged"
    assert row["status"] == "queued"


def test_a_successful_launch_consumes_the_handoff(launch_app):
    """Otherwise the card keeps offering a prompt that is already running."""
    c, _, _, fake = launch_app
    pid = c.post("/api/handoff", json=body("h1")).json()["project_id"]

    assert c.post("/api/launch", json={"project_path": DEMO}).json()["outcome"] == "started"

    assert c.get(f"/api/handoff/{pid}").status_code == 204
    # Consumption and its journal record are the launcher's, which is why the id
    # reaching it matters more here than the status write itself.
    assert fake.calls[0][1] == "h1"


def test_a_launch_with_no_prompt_uses_the_queued_handoff(launch_app):
    """`bridge launch` sends no prompt at all; this pins that contract."""
    c, _, _, fake = launch_app
    c.post("/api/handoff", json=body("h1", prompt="carry on from here"))

    r = c.post("/api/launch", json={"project_path": DEMO, "mode": "background",
                                    "model": "opus", "effort": "high"})

    assert r.status_code == 200, r.text
    spec, handoff_id = fake.calls[0]
    assert spec.prompt == "carry on from here"
    assert handoff_id == "h1"
    assert (spec.mode, spec.model, spec.effort) == ("background", "opus", "high")
    assert spec.title == "a summary"  # defaulted from the handoff's summary
    assert spec.session_id is None  # minted by the launcher, never by the client


def test_a_launch_with_nothing_queued_and_no_prompt_is_a_clear_error(launch_app):
    """Not a 500, and not an empty session either."""
    c, store, _, fake = launch_app
    store.upsert_project(DEMO, "demo")

    r = c.post("/api/launch", json={"project_path": DEMO})

    assert r.status_code == 422
    assert "queued" in r.json()["detail"].lower()
    assert fake.calls == [], "a refused launch must not reach the launcher"

    # Same answer for a project that was never indexed, and the refusal does not
    # bring its row into existence on the way out.
    unknown = "/Users/mitsheth/dev/never-indexed"
    assert c.post("/api/launch", json={"project_path": unknown}).status_code == 422
    assert store.project_by_path(unknown) is None


def test_a_refused_launch_is_422_where_a_failed_spawn_is_200(launch_app):
    """A `LaunchError` is raised *before* the `launches` row exists.

    So there is no launch id and no outcome to report, and answering
    `200 {outcome: 'failed'}` would describe a launch the database does not have.
    A failed *spawn* is the other side of that line and stays a 200, above.
    """
    c, store, _, fake = launch_app
    pid = c.post("/api/handoff", json=body("h1")).json()["project_id"]
    fake.result = launcher.LaunchError("claude is not on PATH; cannot launch a session")

    r = c.post("/api/launch", json={"project_path": DEMO})

    assert r.status_code == 422
    assert "not on PATH" in r.json()["detail"]
    # Nothing recorded, and nothing consumed.
    assert store.launches(pid) == []
    assert c.get(f"/api/handoff/{pid}").json()["id"] == "h1"


def test_a_launch_naming_an_unknown_handoff_is_404_not_a_foreign_key_500(launch_app):
    """`launches.handoff_id` has a foreign key, so this must be caught up front."""
    c, _, _, fake = launch_app

    r = c.post("/api/launch", json={"project_path": DEMO, "prompt": "ad hoc",
                                    "handoff_id": "never-existed"})

    assert r.status_code == 404
    assert fake.calls == []


def test_an_unknown_mode_is_422_and_never_reaches_the_launcher(launch_app):
    c, _, _, fake = launch_app
    c.post("/api/handoff", json=body("h1"))

    r = c.post("/api/launch", json={"project_path": DEMO, "mode": "tmux"})

    assert r.status_code == 422
    assert fake.calls == []
    # And the default is terminal, so the CLI and the card can both omit it.
    c.post("/api/launch", json={"project_path": DEMO})
    assert fake.calls[0][0].mode == "terminal"


# --- Phase 3: the launch band on the card ------------------------------------


def test_the_editable_prompt_is_html_escaped_in_the_textarea(launch_app):
    """A textarea escapes differently from a <pre>, so this is its own test.

    An unescaped `</textarea>` closes the field early and everything after it
    becomes live markup in the document — the same injection the <pre> test
    covers, through a hole that test cannot see.
    """
    c, _, _, _ = launch_app
    prompt = "before </textarea><script>alert('xss')</script> after"
    c.post("/api/handoff", json=body("h1", prompt=prompt))

    html = c.get("/").text

    assert "</textarea><script>" not in html, "the prompt closed its own field"
    assert "&lt;/textarea&gt;&lt;script&gt;" in html
    assert html.count("</textarea>") == 1, "exactly one field was opened and closed"


def test_both_launch_selects_are_labelled_and_preselect_the_suggestion(launch_app):
    c, _, _, _ = launch_app
    pid = c.post(
        "/api/handoff",
        json=body("h1", suggested_model="sonnet", suggested_effort="xhigh"),
    ).json()["project_id"]

    html = c.get("/").text
    lid = f"launch-{pid}"

    assert f'<label class="launch__label" for="{lid}-model">Model</label>' in html
    assert f'<label class="launch__label" for="{lid}-effort">Effort</label>' in html
    assert f'id="{lid}-model"' in html
    assert f'id="{lid}-effort"' in html
    # The value is what reaches `--model`; the label is what a human reads.
    # Emitting the label as the value would send `--model "sonnet — latest
    # (Sonnet 5)"` and fail the launch.
    assert '<option value="sonnet" selected>sonnet — latest (Sonnet 5)</option>' in html
    assert '<option value="xhigh" selected>xhigh</option>' in html
    # Three selects now: model, effort, permissions. The permission select is
    # always preselected on its first option, which is the no-flag one.
    assert html.count(" selected>") == 3, "one preselection per select, no more"
    assert '<option value="" selected>Ask as usual</option>' in html


def test_a_suggestion_the_config_does_not_list_is_still_preselected(launch_app):
    """The suggestion comes from a real session's model string, which need not be
    one of the configured short names. Dropping it would silently launch a
    different model than the one being suggested."""
    c, _, _, _ = launch_app
    # Deliberately NOT `body()`'s default: Phase 4 added `claude-opus-5` to the
    # catalog, so the old default silently stopped being off-catalog and this
    # test stopped testing the prepend. Pin a value the catalog does not list.
    off_catalog = "claude-opus-4-2"
    pid = c.post(
        "/api/handoff", json=body("h1", suggested_model=off_catalog)
    ).json()["project_id"]
    assert off_catalog not in [m.value for m in load({}).models]

    html = c.get("/").text

    assert f'<option value="{off_catalog}" selected>{off_catalog}</option>' in html
    assert f'id="launch-{pid}-model"' in html


def test_with_no_suggestion_the_first_catalog_entry_is_selected(launch_app):
    """Without an explicit `selected` the browser silently picks option one
    anyway, so the preselection becomes invisible rather than absent — and a
    later reorder of the catalog would change what launches with no warning.
    """
    c, store, _, _ = launch_app
    store.upsert_project("/Users/mitsheth/dev/nohandoff", "nohandoff")

    html = c.get("/").text

    first = load({}).models[0]
    assert (
        f'<option value="{first.value}" selected>{first.label}</option>' in html
    )


def test_two_cards_produce_no_duplicate_element_id(launch_app):
    """Ids are keyed off `project_id`; keying them off the handoff id would emit a
    bare prefix on the card with nothing queued and collide across cards."""
    c, store, _, _ = launch_app
    c.post("/api/handoff", json=body("h1", path=DEMO))
    store.upsert_project("/Users/mitsheth/dev/second", "second")

    html = c.get("/").text

    ids = re.findall(r'\sid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), f"duplicate ids: {sorted(ids)}"
    assert len([i for i in ids if i.startswith("launch-")]) == 6, (
        "three selects on each of the two cards"
    )


def test_a_card_with_no_queued_handoff_still_renders_a_launch_band(launch_app):
    """Nothing queued is not the same as nothing to launch."""
    c, store, _, _ = launch_app
    pid = store.upsert_project("/Users/mitsheth/dev/quiet", "quiet")

    html = c.get("/").text

    assert f'data-launch="launch-{pid}"' in html
    assert f'id="launch-{pid}-model"' in html
    assert f'data-launch-status="launch-{pid}"' in html
    # ...and no empty prompt block, no orphan copy affordance, no handoff id.
    assert "Next step queued" not in html
    assert "<textarea" not in html
    assert "data-copy-target" not in html
    assert "data-launch-handoff" not in html


def test_every_new_control_is_labelled_and_none_leaves_the_tab_order(launch_app):
    """Keyboard operability asserted structurally rather than by hand."""
    c, _, _, _ = launch_app
    c.post("/api/handoff", json=body("h1"))

    html = c.get("/").text

    assert 'tabindex="-1"' not in html
    labelled = set(re.findall(r'<label[^>]*\sfor="([^"]+)"', html))
    fields = re.findall(r"<(?:select|textarea)\b[^>]*>", html)
    assert len(fields) == 4, "three selects and the prompt field"
    for tag in fields:
        ident = re.search(r'\sid="([^"]+)"', tag)
        assert (ident and ident.group(1) in labelled) or "aria-label=" in tag, tag
    # The ▶ has no text node of its own, so its accessible name is explicit.
    assert re.search(
        r'<button[^>]*data-launch-button[^>]*aria-label="Launch a session for [^"]+"',
        html,
    )
    # The launch status is a live region with a key of its own, so it and the
    # copy status cannot overwrite each other.
    assert 'data-launch-status="launch-' in html
    assert "data-copy-status=" in html


def test_the_project_page_lists_launch_history_with_its_linked_session(launch_app):
    """Task 7 added the table; the route never passed `launches`, so it was inert."""
    c, store, _, _ = launch_app
    pid = store.upsert_project(DEMO, "demo")
    store.upsert_session(
        SessionRecord(session_id=SESSION_ID, transcript_path="/t/launched.jsonl",
                      title="Launched work", ended_at="2026-07-30T10:00:00.000Z"),
        pid,
    )
    store.create_launch(
        Launch(id="l-linked", project_id=pid, mode="terminal", model="opus",
               effort="xhigh", prompt="go", session_id=SESSION_ID,
               launched_at=2000, outcome="started")
    )
    store.create_launch(
        Launch(id="l-orphan", project_id=pid, mode="background", model="sonnet",
               effort="low", prompt="go too", launched_at=1000, outcome="started")
    )

    html = c.get(f"/project/{pid}").text

    assert "Launches, most recent first" in html
    assert "terminal" in html
    assert "background" in html
    assert "opus/xhigh" in html
    assert "sonnet/low" in html
    assert "started" in html
    assert "Launched work" in html, "the linked session is shown as its own title"
    # A launch whose session never appeared is not an error; it stays visible.
    assert "no session yet" in html


# --- Phase 4 Task 2: permission modes ----------------------------------------


def test_a_launch_with_no_permission_mode_reaches_the_spec_as_none(launch_app):
    """The default must survive the whole way to the LaunchSpec, not be
    reconstituted into a benign-looking mode somewhere in the middle."""
    c, _, _, launch_fn = launch_app
    c.post("/api/handoff", json=body("h1"))
    c.post("/api/launch", json={"project_path": DEMO})
    spec, _ = launch_fn.calls[-1]
    assert spec.permission_mode is None


def test_the_requested_permission_mode_reaches_the_spec_verbatim(launch_app):
    c, _, _, launch_fn = launch_app
    c.post("/api/handoff", json=body("h1"))
    c.post("/api/launch",
           json={"project_path": DEMO, "permission_mode": "bypassPermissions"})
    spec, _ = launch_fn.calls[-1]
    assert spec.permission_mode == "bypassPermissions"


def test_an_unknown_permission_mode_is_refused_before_anything_is_launched(launch_app):
    """422 at the edge, and -- the part that matters -- no launch recorded."""
    c, _, _, launch_fn = launch_app
    c.post("/api/handoff", json=body("h1"))
    before = len(launch_fn.calls)
    response = c.post("/api/launch",
                      json={"project_path": DEMO, "permission_mode": "yolo"})
    assert response.status_code == 422
    assert len(launch_fn.calls) == before, "a refused mode still spawned something"


def test_no_handoff_field_can_arm_a_permission_mode(launch_app):
    """A handoff may suggest a model and an effort. It must never be able to
    suggest a permission mode: an authored brief that could pre-arm a bypass is
    exactly the sticky default the design forbids.
    """
    c, _, _, _ = launch_app
    c.post("/api/handoff", json=body("h1"))
    html = c.get("/").text

    # The rendered select always lands on the no-flag option, whatever the
    # handoff says, and the dangerous option is never the selected one.
    assert '<option value="" selected>Ask as usual</option>' in html
    assert 'value="bypassPermissions" class="launch__option--danger">' in html
    assert '"bypassPermissions" selected' not in html
    # And the field is not among the things a handoff can carry at all.
    from bridge.api import HandoffIn

    assert not any("permission" in f for f in HandoffIn.model_fields)


def test_the_permission_select_is_labelled_and_marked_dangerous(launch_app):
    c, _, _, _ = launch_app
    c.post("/api/handoff", json=body("h1"))
    html = c.get("/").text
    assert re.search(r'<label[^>]*for="launch-\d+-perm">Permissions</label>', html)
    # Colour is never the only signal: the option says so in words.
    assert "SKIP ALL CHECKS" in html


# --- Phase 4 Task 4: the last good git state renders with its age ------------


def test_a_stale_git_probe_renders_the_last_good_state_and_its_age(tmp_path):
    """Rendered rather than asserted on the dataclass, because the filter choice
    is the bug: `ago` takes an ISO-8601 string and `cached_at` is an epoch int,
    so the wrong one either raises or renders nonsense."""
    from bridge.models import GitState

    import bridge.cards as cards_mod

    cfg = load({"db_path": tmp_path / "g.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/cached", "cached")
    store.upsert_session(
        SessionRecord(session_id="s-cached", transcript_path="/t/s-cached",
                      title="Work", ended_at="2026-07-30T10:00:00.000Z"),
        pid,
    )
    orig = cards_mod.gitprobe.probe
    try:
        cards_mod.gitprobe.probe = lambda p: GitState(status="ok", branch="cached-branch")
        c = TestClient(create_app(store, cfg))
        assert "cached-branch" in c.get("/").text

        cards_mod.gitprobe.probe = lambda p: GitState(status="unavailable")
        text = c.get("/").text
        assert "cached-branch" in text, "the last good branch was not shown"
        assert "as of" in text
        assert "git unavailable" not in text
    finally:
        cards_mod.gitprobe.probe = orig
        store.close()


# --- Phase 4 Task 7: diagnostics ---------------------------------------------


def test_diagnostics_survives_never_having_indexed(client):
    """A fresh install has no runs; the route must answer, not 500."""
    c, _, _ = client
    r = c.get("/api/diagnostics")
    assert r.status_code == 200
    assert r.json()["last_index"] is None
    assert r.json()["parse_errors"] == 0


def test_an_index_run_is_recorded_so_diagnostics_has_something_to_read(client):
    c, _, _ = client
    c.post("/api/refresh")
    body = c.get("/api/diagnostics").json()
    assert body["last_index"] is not None
    assert body["last_index"]["duration_ms"] >= 0


def test_diagnostics_reports_parse_errors_from_the_last_run(client):
    c, store, _ = client
    store.record_index_run({"parse_errors": 3, "files_seen": 9},
                           ran_at=100, duration_ms=5)
    assert c.get("/api/diagnostics").json()["parse_errors"] == 3


def test_diagnostics_reads_the_LATEST_run_not_the_first(client):
    c, store, _ = client
    store.record_index_run({"parse_errors": 7}, ran_at=100, duration_ms=1)
    store.record_index_run({"parse_errors": 0}, ran_at=200, duration_ms=1)
    assert c.get("/api/diagnostics").json()["parse_errors"] == 0


def test_diagnostics_counts_undrained_spool_files(tmp_path):
    cfg = load({"db_path": tmp_path / "d.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    c = TestClient(create_app(store, cfg))
    # The live outbox IS `spool_dir`; `drained/` and `bad/` sit under it.
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    (cfg.spool_dir / "x.json").write_text("{}")
    assert c.get("/api/diagnostics").json()["spool_depth"] == 1
    store.close()


def test_drained_spool_files_are_not_counted_as_depth(tmp_path):
    """`spool/drained/` is history, not backlog. Counting it makes the depth
    grow forever and permanently claim a backlog that was drained."""
    cfg = load({"db_path": tmp_path / "d2.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    c = TestClient(create_app(store, cfg))
    (cfg.spool_dir / "drained").mkdir(parents=True, exist_ok=True)
    (cfg.spool_dir / "drained" / "x.json").write_text("{}")
    assert c.get("/api/diagnostics").json()["spool_depth"] == 0
    store.close()


def test_diagnostics_records_which_sensor_answered_and_what_version(client):
    """When the schema next drifts this is the difference between a diagnosis
    and a bisect."""
    body = client[0].get("/api/diagnostics").json()
    assert body["live_source"] in ("registry", "subprocess", "none")
    assert "claude_version" in body


def test_diagnostics_counts_only_still_queued_handoffs(client):
    c, store, pid = client
    store.create_handoff(Handoff(id="dq1", project_path=DEMO,
                                 next_prompt="p", created_at=1), pid)
    assert c.get("/api/diagnostics").json()["queued_handoffs"] == 1
    store.set_handoff_status("dq1", "consumed")
    assert c.get("/api/diagnostics").json()["queued_handoffs"] == 0


def test_the_header_links_to_diagnostics_only_when_something_is_wrong(client):
    """A permanent link would train the eye to ignore it."""
    c, store, _ = client
    store.record_index_run({"parse_errors": 0}, ran_at=1, duration_ms=1)
    assert "data-diagnostics-alert" not in c.get("/").text
    store.record_index_run({"parse_errors": 2}, ran_at=2, duration_ms=1)
    assert "data-diagnostics-alert" in c.get("/").text


def test_the_diagnostics_page_renders_and_says_so_in_words(client):
    c, store, _ = client
    store.record_index_run({"parse_errors": 2, "files_seen": 4},
                           ran_at=2, duration_ms=7)
    text = c.get("/diagnostics").text
    assert "Diagnostics" in text
    assert "Parse errors" in text
    # Status is never colour alone.
    assert "needs attention" in text


def test_a_diagnostics_write_failure_cannot_fail_an_index(client, monkeypatch):
    """Indexing is the one thing that must always work."""
    c, store, _ = client

    def boom(*a, **k):
        raise RuntimeError("diagnostics exploded")

    monkeypatch.setattr(store, "record_index_run", boom)
    assert c.post("/api/refresh").status_code == 200


def test_diagnostics_reports_terminal_agents_as_not_running(tmp_path, monkeypatch):
    """A background agent that is `done` occupies nothing. Counting it would
    inflate "running sessions" forever after the work finished."""
    from bridge import agents
    from bridge.models import AgentsState, LiveSession

    def fake_probe(*a, **k):
        return AgentsState(status="ok", source="registry", sessions=[
            LiveSession(session_id="aaaaaaaa-0000-0000-0000-000000000001",
                        cwd="/p", kind="background", status="done"),
            LiveSession(session_id="aaaaaaaa-0000-0000-0000-000000000002",
                        cwd="/p", kind="background", status="working"),
        ])

    monkeypatch.setattr(agents, "probe", fake_probe)
    cfg = load({"db_path": tmp_path / "t.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    c = TestClient(create_app(store, cfg))
    assert c.get("/api/diagnostics").json()["running_sessions"] == 1
    store.close()


# --- Phase 4 Task 8: SSE -----------------------------------------------------


def _frames(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if name:
            out.append((name, data))
    return out


def test_events_opens_with_a_full_snapshot_in_sse_frame_format(client):
    c, _, _ = client
    with c.stream("GET", "/events?max_ticks=1&interval=0") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        body = "".join(response.iter_text())

    assert body.startswith("event: snapshot\n")
    # The trailing BLANK line terminates the frame. With a single "\n" the
    # browser buffers forever and no event ever fires, with no error anywhere.
    assert body.endswith("\n\n")
    name, payload = _frames(body)[0]
    assert name == "snapshot"
    assert "live" in payload


def test_every_reconnect_begins_with_a_snapshot_so_no_replay_is_needed(client):
    """This is why there is no Last-Event-ID handling to write."""
    c, _, _ = client
    for _ in range(2):
        with c.stream("GET", "/events?max_ticks=1&interval=0") as r:
            assert _frames("".join(r.iter_text()))[0][0] == "snapshot"


def test_an_unchanged_tick_emits_nothing_after_the_snapshot(client):
    c, _, _ = client
    with c.stream("GET", "/events?max_ticks=4&interval=0") as r:
        frames = _frames("".join(r.iter_text()))
    assert [n for n, _ in frames] == ["snapshot"], "a quiet server still emitted"


def test_a_capped_stream_ends_with_a_named_refresh_rather_than_running_forever(client):
    """`max_ticks` is a BACKSTOP, not the thing under test.

    Without it, a build that has lost the time cap streams forever at
    interval=0 and this test hangs instead of failing -- which is exactly what
    it did under mutation, taking the falsifier down with it. With the backstop
    the cap is still what produces the `refresh`, and its absence fails fast.
    """
    c, _, _ = client
    with c.stream("GET", "/events?interval=0&max_seconds=0&max_ticks=5") as r:
        frames = _frames("".join(r.iter_text()))
    assert [n for n, _ in frames] == ["snapshot", "refresh"]


def test_a_delta_carries_a_tombstone_when_a_session_ends(tmp_path, monkeypatch):
    """The planned payload could say "busy" but had no way to say "gone", so a
    card kept its live band until the page was reloaded."""
    from bridge import agents
    from bridge.models import AgentsState, LiveSession

    cfg = load({"db_path": tmp_path / "sse.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    store.upsert_project("/p/gone", "gone")
    states = [
        AgentsState(status="ok", sessions=[LiveSession(
            session_id="aaaaaaaa-0000-0000-0000-000000000001", cwd="/p/gone",
            kind="interactive", status="busy", started_at=5)]),
        AgentsState(status="ok", sessions=[]),
    ]
    monkeypatch.setattr(agents, "probe", lambda *a, **k: states.pop(0) if states
                        else AgentsState(status="ok", sessions=[]))
    c = TestClient(create_app(store, cfg))
    with c.stream("GET", "/events?max_ticks=2&interval=0") as r:
        frames = _frames("".join(r.iter_text()))
    store.close()

    assert [n for n, _ in frames] == ["snapshot", "delta"]
    assert frames[0][1]["live"]["/p/gone"]["status"] == "busy"
    assert frames[1][1]["removed"] == ["/p/gone"]


def test_the_wire_payload_excludes_unattributed_sessions(tmp_path, monkeypatch):
    """A session in no registered project has no card to patch. Putting it on
    the wire keyed by its own cwd would make the client look for a band that
    does not exist -- or, worse, find an unrelated one."""
    from bridge import agents
    from bridge.models import AgentsState, LiveSession

    cfg = load({"db_path": tmp_path / "un.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    store.upsert_project("/p/real", "real")
    monkeypatch.setattr(agents, "probe", lambda *a, **k: AgentsState(
        status="ok", sessions=[
            LiveSession(session_id="aaaaaaaa-0000-0000-0000-000000000001",
                        cwd="/p/real", kind="interactive", status="busy"),
            LiveSession(session_id="aaaaaaaa-0000-0000-0000-000000000002",
                        cwd="/somewhere/unregistered", kind="interactive",
                        status="busy"),
        ]))
    c = TestClient(create_app(store, cfg))
    with c.stream("GET", "/events?max_ticks=1&interval=0") as r:
        payload = _frames("".join(r.iter_text()))[0][1]
    store.close()

    assert list(payload["live"]) == ["/p/real"]
    assert agents.UNATTRIBUTED not in payload["live"]


def test_the_stream_never_holds_the_store_lock_while_it_sleeps(client, monkeypatch):
    """The deterministic guard for the worst regression in this phase.

    The timing test below is a smoke check only, and it cannot see this:
    TestClient consumes a streaming body by PULLING, so between frames the
    generator is parked on its yield and has not reached the sleep at all. A
    concurrent request therefore never overlaps the sleep, and the assertion
    passes even when the lock is held across it.

    So the property is asserted where it lives instead. `Store._lock` is an
    RLock, which the owning thread can re-acquire freely -- the check has to
    come from a different thread or it proves nothing.
    """
    import threading

    c, store, _ = client
    free_during_sleep = []

    def probing_sleep(_seconds):
        got = []

        def try_acquire():
            acquired = store._lock.acquire(blocking=False)
            got.append(acquired)
            if acquired:
                store._lock.release()  # RLock: released by its owning thread

        thread = threading.Thread(target=try_acquire)
        thread.start()
        thread.join()
        free_during_sleep.append(bool(got and got[0]))

    monkeypatch.setattr("bridge.api.time.sleep", probing_sleep)
    with c.stream("GET", "/events?max_ticks=3&interval=0") as r:
        "".join(r.iter_text())

    assert free_during_sleep, "the stream never slept, so nothing was proved"
    assert all(free_during_sleep), (
        "the store lock was held across the stream's sleep: every other route "
        "would block while a tab is open"
    )


def test_an_open_stream_does_not_block_other_requests(client):
    """The store is ONE connection behind ONE lock. A stream that holds it
    across its sleep freezes the entire panel."""
    import time as _time

    c, _, _ = client
    with c.stream("GET", "/events?max_ticks=3&interval=0.4") as response:
        chunks = response.iter_text()
        # Pull the first frame. Without this the generator is still parked on
        # its opening yield and has not reached the sleep at all, so the
        # assertion below passes even when the sleep holds the lock -- which is
        # exactly how this test let that mutation survive.
        next(chunks)
        start = _time.monotonic()
        assert c.get("/api/projects").status_code == 200
        elapsed = _time.monotonic() - start
    assert elapsed < 0.3, f"a request waited {elapsed:.2f}s behind the stream"


def test_the_stream_never_writes(client):
    """`/events` reads. It must not index, launch, or write."""
    c, store, _ = client
    before = store.latest_index_run()
    with c.stream("GET", "/events?max_ticks=2&interval=0") as r:
        "".join(r.iter_text())
    after = store.latest_index_run()
    assert (before is None) == (after is None)


def test_live_js_never_touches_the_prompt_textarea():
    """The handoff prompt is the only state Bridge cannot rebuild."""
    source = (Path(__file__).resolve().parent.parent / "src" / "bridge"
              / "static" / "live.js").read_text()
    assert "data-prompt-handoff" not in source
    assert ".innerHTML" not in source        # no subtree replacement
    assert "location.reload" not in source
    # No replay handling: `lastEventId` is the EventSource property a
    # replay design would have to read, and every reconnect already opens
    # with a full snapshot. Asserted on the API name, not on the prose --
    # the header is named in a comment explaining why it is absent.
    assert "lastEventId" not in source


def test_live_js_handles_all_three_named_events():
    source = (Path(__file__).resolve().parent.parent / "src" / "bridge"
              / "static" / "live.js").read_text()
    for name in ("snapshot", "delta", "refresh"):
        assert f'"{name}"' in source
    assert "removed" in source               # the tombstone is applied


# --- PATCH /api/projects/{id}: hide, archive, restore -------------------------


def test_hiding_a_project_removes_it_from_the_dashboard(client):
    c, store, pid = client
    assert "demo" in c.get("/").text

    r = c.patch(f"/api/projects/{pid}", json={"status": "hidden"})
    assert r.status_code == 200
    assert r.json()["status"] == "hidden"

    body = c.get("/").text
    assert not re.search(r'<h2><a href="/project/\d+">demo</a></h2>', body), (
        "the hidden project still renders a card"
    )
    assert [p["name"] for p in c.get("/api/projects").json()] == []


def test_a_hidden_project_is_still_listed_so_it_can_be_restored(client):
    """Hiding must not be a one-way door.

    `store.projects()` whitelists `active`, so without the list at the foot of
    the dashboard nothing in the panel could name a hidden project again.
    """
    c, _, pid = client
    c.patch(f"/api/projects/{pid}", json={"status": "hidden"})

    body = c.get("/").text
    assert f'data-hidden-project="{pid}"' in body
    assert f'data-project-restore="{pid}"' in body
    # The word, so a project archived by config.toml is distinguishable from one
    # hidden here -- which is the only thing that explains why it vanished.
    assert re.search(
        rf'data-hidden-project="{pid}".*?<span class="card__note">hidden</span>',
        body, re.S,
    )


def test_restoring_a_project_brings_its_card_back(client):
    c, _, pid = client
    c.patch(f"/api/projects/{pid}", json={"status": "hidden"})

    r = c.patch(f"/api/projects/{pid}", json={"status": "active"})
    assert r.status_code == 200
    assert r.json()["status"] == "active"

    body = c.get("/").text
    assert re.search(r'<h2><a href="/project/\d+">demo</a></h2>', body)
    assert f'data-hidden-project="{pid}"' not in body


def test_an_archived_project_is_listed_as_archived_not_as_hidden(client):
    c, _, pid = client
    c.patch(f"/api/projects/{pid}", json={"status": "archived"})
    assert re.search(
        rf'data-hidden-project="{pid}".*?<span class="card__note">archived</span>',
        c.get("/").text, re.S,
    )


def test_patching_an_unknown_project_is_404_not_a_silent_success(client):
    """`set_project_status` is a bare UPDATE with no rowcount check.

    Without the existence check this is a 200 that changed nothing, which at the
    far end of a `fetch()` is indistinguishable from a hide that worked.
    """
    c, _, _ = client
    r = c.patch("/api/projects/99999", json={"status": "hidden"})
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown project"


def test_an_unknown_status_is_refused(client):
    """The vocabulary is a Literal, so a typo cannot invent a status that
    `Store.projects` would then filter out forever."""
    c, _, pid = client
    assert c.patch(f"/api/projects/{pid}", json={"status": "hiden"}).status_code == 422


def test_a_project_patch_with_no_status_is_422_not_a_silent_no_op(client):
    c, _, pid = client
    assert c.patch(f"/api/projects/{pid}", json={}).status_code == 422


def test_every_card_offers_a_hide_control(client):
    c, _, pid = client
    body = c.get("/").text
    assert f'data-project-hide="{pid}"' in body
    # The row the client removes on success, and the region it reports into if
    # the PATCH fails.
    assert f'data-project-card="{pid}"' in body
    assert f'data-project-status="{pid}"' in body


def test_the_hidden_list_is_rendered_even_when_empty(client):
    """Hiding the FIRST project needs somewhere to put it.

    Omitting the block until a reload produced one would leave that project
    unreachable in the meantime -- exactly the one-way door the list prevents.
    """
    c, _, _ = client
    body = c.get("/").text
    assert "data-hidden-projects" in body
    assert "data-hidden-list" in body
    assert re.search(r"<details[^>]*data-hidden-projects[^>]*\shidden[\s>]", body), (
        "the empty list must be present but not visible"
    )


def test_projects_js_never_reloads_over_a_half_typed_prompt():
    """`launch.js` saves on `focusout`, so clicking Hide puts a PATCH in flight
    that a reload would race, losing the one thing Bridge cannot rebuild."""
    source = (Path(__file__).resolve().parent.parent / "src" / "bridge"
              / "static" / "projects.js").read_text()
    assert "location.reload" not in source
    assert ".innerHTML" not in source
