import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bridge import launcher, spool
from bridge.api import DASHBOARD_TERMINAL_SCHEDULES, create_app
from bridge.config import load
from bridge.models import GitState, Handoff, Launch, ScheduledRun, SessionRecord
from bridge.registry import resolve_project
from bridge.store import Store, now_epoch

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


# --- Phase 5: the detail page pays for the probes that already ran -----------


def test_the_project_page_shows_the_cached_git_log(client):
    """spec:394 -- recent git log on the detail page, fed by the `git_cache`
    the card build already wrote. `behind` and the last-commit fields are
    probed on every card build and were rendered nowhere; this collects them.
    The route only READS the cache -- no new probe on the detail path."""
    c, store, pid = client
    store.put_git_cache(
        pid,
        GitState(
            status="ok", branch="main", dirty_count=2, ahead=1, behind=3,
            last_commit_summary="Wire the detail git log",
            last_commit_at=1_780_000_000,
        ),
        probed_at=1_780_000_500,
    )

    html = c.get(f"/project/{pid}").text

    assert "Wire the detail git log" in html, "last commit summary is shown"
    assert "3 behind" in html, "the behind count finally renders somewhere"
    assert "main" in html


def test_the_project_page_breaks_down_session_tokens_including_sidechain(client):
    """spec:395 -- per-session token breakdown with sidechain subtotals. The
    cache and sidechain columns are already on the row (`SELECT *`); only the
    template was throwing them away."""
    c, store, pid = client
    store.upsert_session(
        SessionRecord(
            session_id="s-tok", transcript_path="/t/s-tok.jsonl",
            project_path="/Users/mitsheth/dev/demo", title="Token session",
            ended_at="2026-07-31T10:00:00.000Z",
            tokens_in=1200, tokens_out=800,
            tokens_cache_create=5000, tokens_cache_read=9000,
            sidechain_tokens=3000,
        ),
        pid,
    )

    html = c.get(f"/project/{pid}").text

    assert "3k sidechain" in html, "the sidechain subtotal renders"
    assert "cache 5kw/9kr" in html, "cache create/read subtotals render"


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
    # `fire()` resolves the alias itself (Task 2), so the spec it hands the
    # launcher already carries the canonical path -- a real terminal launch
    # `cd`s into `spec.project_path` directly, and `cd`-ing into the OLD path
    # here would try to enter a directory that no longer exists.
    spec, handoff_id = fake.calls[0]
    assert spec.project_path == "/Users/mitsheth/dev/projectX"
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


def test_fire_resolves_alias_and_passes_the_snapshot_to_launch_fn(client, tmp_path):
    """A future scheduler calls `api.fire` directly, with no route in front of it
    to resolve the alias table first -- so `fire` has to do that resolution
    itself before building the `LaunchSpec` the launcher receives.
    """
    from bridge import api

    _, store, _ = client
    cfg = load({"db_path": tmp_path / "fire.db", "spool_dir": tmp_path / "spool"})
    store.set_alias("/old/path", "/Users/mitsheth/dev/demo")
    calls = []

    def fake_launch(store, cfg, spec, handoff_id=None, **kwargs):
        calls.append(spec)
        return launcher.LaunchResult("L1", "started")

    result = api.fire(
        store, cfg,
        project_path="/old/path",
        prompt="scheduled prompt",
        mode="terminal",
        model=None,
        effort=None,
        permission_mode="acceptEdits",
        title="scheduled",
        handoff_id=None,
        launch_fn=fake_launch,
    )

    assert result.outcome == "started"
    assert calls[0].project_path == "/Users/mitsheth/dev/demo"  # alias resolved
    assert calls[0].mode == "terminal"
    assert calls[0].permission_mode == "acceptEdits"


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
    # Balanced, not a fixed count: Task 5's compose box adds a second, always-
    # empty textarea to every card, so the number that matters is that every
    # opened field was also closed -- an unescaped `</textarea>` in the prompt
    # would leave one dangling open (or one bare close with nothing to match).
    assert html.count("<textarea") == html.count("</textarea>"), (
        "every opened field must also be closed"
    )


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
    # always preselected on its first option, which is the no-flag one. Scoped
    # to the launch band itself -- Task 5's compose box and schedule forms add
    # their own preselected "terminal" mode options elsewhere on the page,
    # which is not what this assertion is about.
    band = re.search(rf'<p class="launch" data-launch="{lid}".*?</p>', html, re.S)
    assert band, "the launch band itself must be present"
    assert band.group(0).count(" selected>") == 3, "one preselection per select, no more"
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
    # ...and no queued-handoff artifacts. The compose box's own textarea is
    # unrelated and expected to be present on every card, handoff or not.
    assert "Next step queued" not in html
    assert "data-prompt-handoff" not in html
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
    # Three launch-band selects and the handoff's own prompt field, plus
    # Task 5's compose box (its prompt field and its own mode select) and the
    # handoff's "Schedule…" reveal (one more mode select).
    assert len(fields) == 7, "three launch selects, two mode selects, two prompts"
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


def test_a_single_sample_busy_to_idle_flap_never_reaches_the_wire(
    tmp_path, monkeypatch
):
    """The hysteresis has to sit in `_live_snapshot`, not only in `build_cards`.

    `LivenessDebouncer` exists so a session that goes quiet for one sample does
    not flicker the card. It was applied on the render path alone, so the SSE
    path re-emitted every flap the render was busy suppressing: the card held
    "running" while a delta told the client "idle", and the two disagreed on the
    same page. `interval=0` puts all three ticks inside the 1.5 s hold.
    """
    from bridge import agents
    from bridge.models import AgentsState, LiveSession

    cfg = load({"db_path": tmp_path / "flap.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    store.upsert_project("/p/flap", "flap")

    def session(status):
        return AgentsState(status="ok", sessions=[LiveSession(
            session_id="aaaaaaaa-0000-0000-0000-000000000001", cwd="/p/flap",
            kind="interactive", status=status, started_at=5)])

    states = [session("busy"), session("idle"), session("idle")]
    monkeypatch.setattr(agents, "probe",
                        lambda *a, **k: states.pop(0) if states else session("idle"))
    c = TestClient(create_app(store, cfg))
    with c.stream("GET", "/events?max_ticks=3&interval=0") as r:
        frames = _frames("".join(r.iter_text()))
    store.close()

    assert frames[0][1]["live"]["/p/flap"]["status"] == "busy"
    assert [n for n, _ in frames] == ["snapshot"], (
        "the idle samples are inside the hold, so nothing changed to report"
    )


def test_the_hold_releases_so_idle_is_delayed_and_not_suppressed(
    tmp_path, monkeypatch
):
    """The other half of the hysteresis, and the worse failure of the two.

    Debouncing the wire payload buys a permanent lie if the hold never expires:
    a finished session would sit at "running" until the page was reloaded, which
    is the state the tombstone work existed to eliminate. The clock is what
    separates a 1.5 s delay from a stuck card, so it is asserted directly.
    """
    from bridge import agents, api
    from bridge.models import AgentsState, LiveSession

    cfg = load({"db_path": tmp_path / "hold.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    store.upsert_project("/p/hold", "hold")

    def session(status):
        return AgentsState(status="ok", sessions=[LiveSession(
            session_id="aaaaaaaa-0000-0000-0000-000000000003", cwd="/p/hold",
            kind="interactive", status=status, started_at=5)])

    states = [session("busy"), session("idle"), session("idle")]
    monkeypatch.setattr(agents, "probe",
                        lambda *a, **k: states.pop(0) if states else session("idle"))
    # Two samples inside the 1.5 s hold, the third well outside it.
    clock = iter([1000, 1000, 1010])
    monkeypatch.setattr(api, "now_epoch", lambda: next(clock, 1010))

    c = TestClient(create_app(store, cfg))
    with c.stream("GET", "/events?max_ticks=3&interval=0") as r:
        frames = _frames("".join(r.iter_text()))
    store.close()

    assert [n for n, _ in frames] == ["snapshot", "delta"], (
        "one delta, on the tick after the hold expired -- not two, and not none"
    )
    assert frames[0][1]["live"]["/p/hold"]["status"] == "busy"
    assert frames[1][1]["live"]["/p/hold"]["status"] == "idle"


def test_the_unattributed_block_holds_the_same_status_the_cards_do(
    tmp_path, monkeypatch
):
    """One debouncer, both consumers.

    An unattributed session is rendered from `_live_snapshot` while a card is
    rendered from `build_cards`. Debouncing only the second means two blocks on
    ONE page disagree about whether the same machine is busy.
    """
    from bridge import agents
    from bridge.models import AgentsState, LiveSession

    cfg = load({"db_path": tmp_path / "un2.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)

    def state(status):
        return AgentsState(status="ok", sessions=[LiveSession(
            session_id="aaaaaaaa-0000-0000-0000-000000000002",
            cwd="/somewhere/unregistered", kind="interactive", status=status)])

    states = [state("busy"), state("idle")]
    monkeypatch.setattr(agents, "probe",
                        lambda *a, **k: states.pop(0) if states else state("idle"))
    c = TestClient(create_app(store, cfg))
    assert 'class="live live--busy"' in c.get("/").text
    second = c.get("/").text
    store.close()

    assert 'class="live live--busy"' in second, (
        "the sensor said idle once, inside the hold, so the block still says busy"
    )


def test_the_wire_payload_keys_unattributed_sessions_by_their_own_cwd(
    tmp_path, monkeypatch
):
    """Supersedes an earlier decision to exclude them, whose two reasons no
    longer hold.

    It excluded them because "a session in no registered project has no card to
    patch" and keying by cwd would make the client hunt for a band that does not
    exist, "or, worse, find an unrelated one". The dashboard now renders those
    bands itself, so the first is false; and an unattributed cwd cannot equal
    any card's `data-live-path`, because `by_project` only puts a session in the
    bucket after it has failed both an exact and a prefix match against every
    registered path — so the second cannot happen either.

    What the exclusion did cost was real: the topbar counts these sessions as
    running, so the count and the cards disagreed with nothing to explain it.
    """
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

    assert sorted(payload["live"]) == ["/p/real", "/somewhere/unregistered"]
    # The sentinel itself must never reach the wire: it is a grouping key, not
    # a path, and a client would try to patch a band named "\x00unattributed".
    assert agents.UNATTRIBUTED not in payload["live"]
    assert payload["unattributed"] == ["/somewhere/unregistered"]


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


def test_a_project_patch_with_neither_field_is_422_not_a_silent_no_op(client):
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


# --- The topbar's global state ------------------------------------------------


def _iso_now() -> str:
    """Inside the 5h window, so the session counts toward the burn rate.

    The fixture's own session is dated two days back on purpose, which is what
    makes the totals below attributable to exactly the row each test adds.
    """
    from datetime import datetime, timezone

    return (datetime.fromtimestamp(now_epoch(), tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.000Z"))


def test_the_topbar_reports_a_burn_rate_over_the_measured_window(client):
    """Tokens per hour across the 5h window, divided by the window's own length.

    A rate, not a share: the plan publishes no total, so there is no
    denominator a percentage could use.
    """
    c, store, pid = client
    store.upsert_session(
        SessionRecord(session_id="burn", transcript_path="/t/burn.jsonl",
                      project_path=DEMO, ended_at=_iso_now(),
                      tokens_in=30_000, tokens_out=20_000),
        pid,
    )
    body = c.get("/").text
    assert re.search(r"<dt>burn</dt><dd>10k/h</dd>", body), (
        "50k over the 5h window is 10k/h"
    )
    assert "% of" not in body            # still no fabricated denominator
    assert "<meter" not in body          # and no gauge implying one


def test_the_topbar_reports_running_sessions_and_queued_handoffs(
    client, monkeypatch
):
    """Both counts must be non-zero and different from each other.

    Asserting `running` is 0 against conftest's empty registry proved nothing:
    a topbar hardcoded to 0 passed it. Two sessions and one handoff is what
    makes the two numbers tell each other apart.
    """
    from bridge import agents
    from bridge.models import AgentsState, LiveSession

    def live(*_a, **_kw):
        return AgentsState(status="ok", sessions=[
            LiveSession(session_id="a", cwd=DEMO, kind="interactive",
                        status="busy"),
            LiveSession(session_id="b", cwd=DEMO, kind="interactive",
                        status="idle"),
            # Terminal, so it is running for nobody and must not be counted.
            LiveSession(session_id="c", cwd=DEMO, kind="background",
                        status="done"),
        ])

    monkeypatch.setattr(agents, "probe", live)

    c, store, pid = client
    store.create_handoff(Handoff(
        id="h-top", project_path=DEMO, next_prompt="go", status="queued",
    ), pid)
    body = c.get("/").text
    assert re.search(r"<dt>queued</dt><dd>1</dd>", body)
    assert re.search(r"<dt>running</dt><dd>2</dd>", body)


def test_the_topbar_says_never_rather_than_leaving_the_index_time_blank(client):
    """An empty cell reads as a rendering fault; a fresh install genuinely has
    not indexed yet."""
    c, _, _ = client
    assert re.search(r"<dt>indexed</dt><dd>never</dd>", c.get("/").text)


def test_the_topbar_reports_the_last_index_time_once_there_is_one(client):
    c, store, _ = client
    store.record_index_run({"files_seen": 1}, ran_at=now_epoch(), duration_ms=1)
    assert re.search(r"<dt>indexed</dt><dd>0m</dd>", c.get("/").text)


# --- Live sessions with no project row ----------------------------------------


def _live_elsewhere(monkeypatch, *cwds):
    """A probe reporting busy sessions in directories with no project row."""
    from bridge import agents
    from bridge.models import AgentsState, LiveSession

    monkeypatch.setattr(agents, "probe", lambda *a, **k: AgentsState(
        status="ok",
        sessions=[LiveSession(session_id=f"u{i}", cwd=c, kind="interactive",
                              status="busy", started_at=1_785_000_000 + i)
                  for i, c in enumerate(cwds)],
    ))


def test_a_session_outside_any_project_is_not_dropped_from_the_stream(
    client, monkeypatch
):
    """`agents.py:300` says these must not be lost, and both consumers lost
    them: the stream skipped the bucket and `build_cards` only ever looks up
    exact project paths."""
    c, _, _ = client
    _live_elsewhere(monkeypatch, "/Users/mitsheth/scratch")

    frames = c.get("/events?max_ticks=1&interval=0").text
    assert "/Users/mitsheth/scratch" in frames, (
        "the session vanished from the live stream entirely"
    )


def test_a_session_outside_any_project_is_shown_with_its_directory(
    client, monkeypatch
):
    """The topbar's running count already includes it, so without this the
    count and the cards disagree and nothing says where the difference went."""
    c, _, _ = client
    _live_elsewhere(monkeypatch, "/Users/mitsheth/scratch")

    body = c.get("/").text
    assert "Running outside any project" in body
    assert "/Users/mitsheth/scratch" in body
    # The same hook the cards use, so live.js patches it knowing nothing about
    # whether it is a project.
    assert 'data-live-path="/Users/mitsheth/scratch"' in body
    # And the count it has to agree with.
    assert re.search(r"<dt>running</dt><dd>1</dd>", body)


def test_a_session_inside_a_project_stays_on_its_card(client, monkeypatch):
    """The bucket must not swallow attributed sessions."""
    c, _, _ = client
    _live_elsewhere(monkeypatch, DEMO)

    body = c.get("/").text
    assert "Running outside any project" not in body
    assert f'data-live-path="{DEMO}"' in body


def test_two_sessions_in_the_same_directory_report_the_newest(client, monkeypatch):
    c, _, _ = client
    _live_elsewhere(monkeypatch, "/Users/mitsheth/scratch", "/Users/mitsheth/scratch")
    body = c.get("/").text
    assert body.count('data-live-path="/Users/mitsheth/scratch"') == 1


# --- Pin ----------------------------------------------------------------------


def test_a_pinned_project_sorts_above_a_queued_handoff(tmp_path):
    """The decision this feature turned on.

    Every other sort term is something Bridge inferred; a pin is the one thing
    the user said outright, and an inference must not overrule an instruction.
    """
    cfg = load({"db_path": tmp_path / "pin.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    quiet = store.upsert_project("/p/quiet", "quiet")
    busy = store.upsert_project("/p/busy", "busy")
    store.create_handoff(Handoff(
        id="h-pin", project_path="/p/busy", next_prompt="go", status="queued",
    ), busy)

    c = TestClient(create_app(store, cfg))
    body = c.get("/").text
    assert body.index(">busy<") < body.index(">quiet<"), "handoff should lead"

    assert c.patch(f"/api/projects/{quiet}", json={"pinned": True}).status_code == 200
    body = c.get("/").text
    assert body.index(">quiet<") < body.index(">busy<"), (
        "the pinned project did not outrank the queued handoff"
    )
    store.close()


def test_pinning_and_hiding_are_independent(client):
    """A caller changing one must not have to restate the other."""
    c, store, pid = client
    c.patch(f"/api/projects/{pid}", json={"pinned": True})
    r = c.patch(f"/api/projects/{pid}", json={"status": "hidden"})
    assert r.json()["pinned"] == 1, "hiding cleared the pin"
    r = c.patch(f"/api/projects/{pid}", json={"pinned": False})
    assert r.json()["status"] == "hidden", "unpinning restored it to the dashboard"


def test_the_card_reports_its_pin_state_to_assistive_technology(client):
    c, _, pid = client
    body = c.get("/").text
    assert f'data-project-pin="{pid}"' in body
    assert re.search(rf'data-project-pin="{pid}"[^>]*aria-pressed="false"', body)
    c.patch(f"/api/projects/{pid}", json={"pinned": True})
    assert re.search(
        rf'data-project-pin="{pid}"[^>]*aria-pressed="true"', c.get("/").text
    )


def test_the_dashboard_probes_liveness_exactly_once(client, monkeypatch):
    """Three probes would observe three different instants and put three
    disagreeing pictures of what is running on one page."""
    from bridge import agents
    from bridge.models import AgentsState

    calls = []
    monkeypatch.setattr(agents, "probe", lambda *a, **k: (
        calls.append(1), AgentsState(status="ok", sessions=[])
    )[1])

    c, _, _ = client
    assert c.get("/").status_code == 200
    assert len(calls) == 1, f"probed {len(calls)} times"


def test_a_failing_sensor_renders_the_dashboard_instead_of_500ing(client, monkeypatch):
    """build_cards guards its own probe, so hoisting the call out had to carry
    the guard with it."""
    from bridge import agents

    def boom(*a, **k):
        raise OSError("sensor down")

    monkeypatch.setattr(agents, "probe", boom)
    c, _, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "demo" in r.text


# --- Phase 6: /api/schedule ---------------------------------------------------


def test_schedule_create_list_and_cancel(client):
    c, store, _ = client
    r = c.post("/api/schedule", json={"project_path": "/Users/mitsheth/dev/demo",
        "prompt": "do it", "scheduled_for": 1000, "mode": "background"})
    assert r.status_code == 201
    jid = r.json()["id"]
    assert any(j["id"] == jid for j in c.get("/api/schedule").json())
    assert c.delete(f"/api/schedule/{jid}").status_code == 200
    assert store.get_scheduled_run(jid)["status"] == "cancelled"


def test_schedule_edit_is_pending_only(client):
    c, store, _ = client
    jid = c.post("/api/schedule", json={"project_path": "/Users/mitsheth/dev/demo",
        "prompt": "x", "scheduled_for": 1000, "mode": "background"}).json()["id"]
    assert c.patch(f"/api/schedule/{jid}", json={"prompt": "y"}).status_code == 200
    store.claim_one_due(now=2000)                      # now 'launching'
    assert c.patch(f"/api/schedule/{jid}", json={"prompt": "z"}).status_code == 409


def test_run_now_claims_and_fires_via_fire(launch_app):
    # launch_app injects a launch_fn double (no real spawn); see existing launch tests
    c, store, _, launch_fn = launch_app
    jid = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "go",
        "scheduled_for": 9_000_000_000, "mode": "background"}).json()["id"]
    r = c.post(f"/api/schedule/{jid}/run-now")
    assert r.status_code == 200
    assert store.get_scheduled_run(jid)["status"] in ("fired", "failed")
    assert c.post(f"/api/schedule/{jid}/run-now").status_code == 409   # not pending anymore


def test_run_now_records_indeterminate_on_an_unexpected_exception(launch_app):
    """A non-`LaunchError` exception out of `fire()` -- a bug, or a
    `sqlite3.IntegrityError` from a handoff deleted between schedule and fire
    -- must not 500 `run-now` or leave the row stuck `launching`.
    `indeterminate` is terminal: the claim already happened, so the row
    really might have spawned, but it must never be auto-retried."""
    c, store, _, fake = launch_app
    jid = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "go",
        "scheduled_for": 9_000_000_000, "mode": "background"}).json()["id"]
    fake.result = RuntimeError("boom")

    r = c.post(f"/api/schedule/{jid}/run-now")

    assert r.status_code == 200
    row = store.get_scheduled_run(jid)
    assert row["status"] == "indeterminate"
    assert "boom" in row["error"]


def test_schedule_rejects_bad_mode_and_prompt(client):
    c, _, _ = client
    assert c.post("/api/schedule", json={"project_path": DEMO, "prompt": "x",
        "scheduled_for": 1000, "mode": "nope"}).status_code == 422
    assert c.post("/api/schedule", json={"project_path": DEMO, "prompt": "a\x00b",
        "scheduled_for": 1000, "mode": "background"}).status_code == 422


def test_schedule_create_rejects_an_unknown_source_handoff(client):
    """Mirrors `post_launch`'s check for `handoff_id`: a made-up id must 404
    at creation, before a row exists, rather than only failing at fire time."""
    c, store, _ = client
    r = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "go",
        "scheduled_for": 1000, "mode": "background",
        "source_handoff_id": "no-such-handoff"})
    assert r.status_code == 404
    assert store.scheduled_runs() == []


def test_schedule_create_rejects_an_out_of_range_scheduled_for(client):
    c, store, _ = client
    r = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "go",
        "scheduled_for": 10**20, "mode": "background"})
    assert r.status_code == 422
    assert store.scheduled_runs() == []


def test_patch_schedule_rejects_an_explicit_null_on_a_required_field(client):
    """`{"prompt": null}` is INCLUDED by `model_dump(exclude_unset=True)`
    because the key was present, unlike an omitted field. Writing that `None`
    into a NOT NULL column would 500; it must 422 instead, leaving the row
    untouched."""
    c, store, _ = client
    jid = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "x",
        "scheduled_for": 1000, "mode": "background"}).json()["id"]

    r = c.patch(f"/api/schedule/{jid}", json={"prompt": None})

    assert r.status_code == 422
    assert store.get_scheduled_run(jid)["prompt"] == "x"


def test_patch_schedule_still_allows_an_omitted_field(client):
    c, store, _ = client
    jid = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "x",
        "scheduled_for": 1000, "mode": "background"}).json()["id"]

    r = c.patch(f"/api/schedule/{jid}", json={"model": "opus"})

    assert r.status_code == 200
    assert r.json()["prompt"] == "x"
    assert r.json()["model"] == "opus"


def test_dashboard_renders_despite_a_row_with_an_extreme_scheduled_for(client):
    """`ScheduleIn` refuses this at creation, but a row seeded before that
    check existed -- or straight through the store -- must still degrade
    gracefully rather than 500 the whole dashboard."""
    c, store, _ = client
    # SQLite's INTEGER column tops out around 9.2e18, so this must be an
    # epoch that fits the column yet still overflows `datetime.fromtimestamp`
    # (year 33658) -- the case a row seeded before `ScheduleIn`'s bound
    # existed could actually carry.
    store.create_scheduled_run(ScheduledRun(
        id="sched-extreme", project_path=DEMO, prompt="x",
        mode="background", scheduled_for=999_999_999_999, created_at=1000,
    ))

    r = c.get("/")

    assert r.status_code == 200
    assert 'data-scheduled-job="sched-extreme"' in r.text


def test_a_launching_scheduled_row_does_not_offer_run_now(client):
    """`run-now` claims a `pending` row; a `launching` one is already claimed,
    so the control there only ever produces a 409."""
    c, store, _ = client
    store.create_scheduled_run(ScheduledRun(
        id="sched-launching", project_path=DEMO, prompt="x",
        mode="terminal", scheduled_for=9_000_000_000, created_at=1000,
    ))
    store.claim_specific("sched-launching")

    body = c.get("/").text

    assert 'data-scheduled-job="sched-launching"' in body
    assert 'data-scheduled-run-now="sched-launching"' not in body


def test_run_now_on_unknown_id_is_404(client):
    c, _, _ = client
    assert c.post("/api/schedule/nope/run-now").status_code == 404


def test_patch_and_delete_on_unknown_id_is_404(client):
    c, _, _ = client
    assert c.patch("/api/schedule/nope", json={"prompt": "y"}).status_code == 404
    assert c.delete("/api/schedule/nope").status_code == 404


def test_run_now_fires_through_fire_with_the_row_snapshot(launch_app):
    """The shared `_fire_claimed_job` tail must call `fire()` with the claimed
    row's own prompt/mode/handoff, not values reconstructed elsewhere."""
    c, store, _, fake = launch_app
    jid = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "scheduled prompt",
        "scheduled_for": 9_000_000_000, "mode": "background",
        "permission_mode": "bypassPermissions"}).json()["id"]

    r = c.post(f"/api/schedule/{jid}/run-now")

    assert r.status_code == 200
    spec, handoff_id = fake.calls[-1]
    assert spec.prompt == "scheduled prompt"
    assert spec.mode == "background"
    assert spec.permission_mode == "bypassPermissions"
    assert handoff_id is None
    row = store.get_scheduled_run(jid)
    assert row["status"] == "fired"
    assert row["launch_id"] == fake.result.launch_id


def test_run_now_records_failure_when_the_launcher_raises(launch_app):
    c, store, _, fake = launch_app
    jid = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "go",
        "scheduled_for": 9_000_000_000, "mode": "background"}).json()["id"]
    fake.result = launcher.LaunchError("no claude on PATH")

    r = c.post(f"/api/schedule/{jid}/run-now")

    assert r.status_code == 200
    row = store.get_scheduled_run(jid)
    assert row["status"] == "failed"
    assert row["error"] == "no claude on PATH"


def test_run_now_records_failure_when_the_launcher_returns_one(launch_app):
    """Distinct from the `LaunchError`-raised path above: here `launch_fn`
    returns normally with `outcome=='failed'` (a spawn that was attempted and
    failed, not one refused before anything ran). Nothing previously drove
    this branch of `_fire_claimed_job`, so an inverted or collapsed
    `result.outcome == "started"` check would still pass the whole suite."""
    c, store, _, fake = launch_app
    jid = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "go",
        "scheduled_for": 9_000_000_000, "mode": "background"}).json()["id"]
    fake.result = launcher.LaunchResult("L9", "failed", error="spawn boom")

    r = c.post(f"/api/schedule/{jid}/run-now")

    assert r.status_code == 200
    row = store.get_scheduled_run(jid)
    assert row["status"] == "failed"
    assert row["launch_id"] == "L9"
    assert row["error"] == "spawn boom"


# --- Task 5: the Scheduled panel section --------------------------------------


def test_dashboard_shows_the_scheduled_section_and_topbar_count_when_a_job_exists(client):
    """Server-rendered from `store.scheduled_runs()`, like every other section:
    the count beside `queued` in the topbar and the job itself must both be
    visible without any JS having run.
    """
    c, store, _ = client
    store.create_scheduled_run(ScheduledRun(
        id="sched-1", project_path=DEMO, prompt="do the thing",
        mode="background", scheduled_for=9_000_000_000,
    ))

    body = c.get("/").text

    assert re.search(r"<dt>scheduled</dt><dd data-topbar-scheduled>1</dd>", body)
    assert 'data-scheduled-job="sched-1"' in body
    assert "demo" in body  # the job's project is named, not just its id
    assert "background" in body
    # Present AND visible -- unlike the empty case below, a real job must not
    # be hidden behind the `hidden` attribute.
    assert not re.search(r"<details[^>]*data-scheduled[^>]*\shidden[\s>]", body)


def test_the_scheduled_section_is_present_but_collapsed_when_empty(client):
    """Mirrors `data-hidden-projects`: always rendered, so a client-side insert
    has somewhere to put the first row, but hidden via the `hidden` attribute
    rather than omitted so an empty dashboard does not show a dead section.
    """
    c, _, _ = client

    body = c.get("/").text

    assert "data-scheduled" in body
    assert re.search(r"<details[^>]*data-scheduled[^>]*\shidden[\s>]", body), (
        "the empty schedule list must be present but not visible"
    )
    assert re.search(r"<dt>scheduled</dt><dd data-topbar-scheduled>0</dd>", body)


def test_the_scheduled_section_offers_edit_cancel_and_run_now_on_a_pending_job(client):
    c, store, _ = client
    store.create_scheduled_run(ScheduledRun(
        id="sched-2", project_path=DEMO, prompt="do it later",
        mode="terminal", scheduled_for=9_000_000_000,
    ))

    body = c.get("/").text

    assert 'data-scheduled-cancel="sched-2"' in body
    assert 'data-scheduled-run-now="sched-2"' in body
    assert 'data-scheduled-edit-toggle="sched-2"' in body


def test_the_scheduled_section_offers_a_retry_affordance_on_a_failed_job(client):
    c, store, _ = client
    store.create_scheduled_run(ScheduledRun(
        id="sched-3", project_path=DEMO, prompt="it blew up",
        mode="terminal", scheduled_for=1000,
    ))
    store.claim_one_due(now=2000)
    store.finish_scheduled_run("sched-3", status="failed", error="no claude on PATH")

    body = c.get("/").text

    assert 'data-scheduled-retry="sched-3"' in body
    assert 'data-scheduled-job="sched-3" class="scheduled__job scheduled__job--failed"' in body


def test_a_cancelled_schedule_does_not_linger_in_the_scheduled_section(client):
    c, store, _ = client
    store.create_scheduled_run(ScheduledRun(
        id="sched-4", project_path=DEMO, prompt="never mind",
        mode="terminal", scheduled_for=9_000_000_000,
    ))
    store.cancel_pending("sched-4")

    body = c.get("/").text

    assert 'data-scheduled-job="sched-4"' not in body
    assert re.search(r"<dt>scheduled</dt><dd data-topbar-scheduled>0</dd>", body)


# --- Follow-up 1 & 2: a retry that keeps the handoff it came from -------------


def _failed_schedule_from_a_handoff(c, store, fake, hid="h-retry"):
    """A schedule born from a queued handoff, fired, and failed."""
    c.post("/api/handoff", json={
        "id": hid, "project_path": DEMO, "next_prompt": "carry on",
    })
    jid = c.post("/api/schedule", json={
        "project_path": DEMO, "prompt": "carry on", "mode": "background",
        "scheduled_for": 9_000_000_000, "source_handoff_id": hid,
    }).json()["id"]
    fake.result = launcher.LaunchError("no claude on PATH")
    c.post(f"/api/schedule/{jid}/run-now")
    assert store.get_scheduled_run(jid)["status"] == "failed"
    fake.result = launcher.LaunchResult("L-retry", "started")
    return jid, hid


def test_retry_refires_a_failed_schedule_with_its_handoff_still_attached(launch_app):
    """The bug this endpoint exists for: the panel's old retry POSTed
    `/api/launch` with no `handoff_id`, so the retry succeeded and the original
    handoff stayed queued forever. `launch_fn` is a double here, so the
    consumption itself never runs -- what is asserted is the contract that
    drives it: the handoff id reaches the launcher.
    """
    c, store, _, fake = launch_app
    jid, hid = _failed_schedule_from_a_handoff(c, store, fake)

    r = c.post(f"/api/schedule/{jid}/retry")

    assert r.status_code == 200
    _spec, handoff_id = fake.calls[-1]
    assert handoff_id == hid
    new = r.json()
    assert new["id"] != jid
    assert new["retry_of"] == jid
    assert new["source_handoff_id"] == hid
    assert new["status"] == "fired"
    assert new["launch_id"] == "L-retry"
    # The original keeps its failure: a retry is a new row, not a rewrite.
    assert store.get_scheduled_run(jid)["status"] == "failed"


def test_retry_recovers_an_indeterminate_job(launch_app):
    """A crash-stranded row is never auto-retried, so without this it had no
    recovery path at all -- `run-now` is gated to `pending`."""
    c, store, _, fake = launch_app
    jid = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "go",
        "scheduled_for": 9_000_000_000, "mode": "background"}).json()["id"]
    store.claim_specific(jid)
    store.reconcile_launching(9_000_000_001, store.launching_scheduled_run_ids())

    r = c.post(f"/api/schedule/{jid}/retry")

    assert r.status_code == 200
    assert r.json()["status"] == "fired"


def test_retry_on_an_unknown_id_is_404(client):
    c, _, _ = client
    assert c.post("/api/schedule/nope/retry").status_code == 404


@pytest.mark.parametrize("status", ["pending", "launching", "fired", "cancelled"])
def test_retry_of_a_job_that_is_not_failed_or_indeterminate_is_409(launch_app, status):
    c, store, _, _ = launch_app
    jid = c.post("/api/schedule", json={"project_path": DEMO, "prompt": "go",
        "scheduled_for": 9_000_000_000, "mode": "background"}).json()["id"]
    if status == "launching":
        store.claim_specific(jid)
    elif status == "cancelled":
        store.cancel_pending(jid)
    elif status == "fired":
        c.post(f"/api/schedule/{jid}/run-now")

    r = c.post(f"/api/schedule/{jid}/retry")

    assert r.status_code == 409
    assert store.count_scheduled_runs() == 1      # nothing new was created


def test_a_second_retry_of_the_same_failure_is_409(launch_app):
    c, store, _, fake = launch_app
    jid, _ = _failed_schedule_from_a_handoff(c, store, fake)
    assert c.post(f"/api/schedule/{jid}/retry").status_code == 200

    assert c.post(f"/api/schedule/{jid}/retry").status_code == 409
    assert store.count_scheduled_runs() == 2      # the original and one retry


def test_the_scheduled_section_offers_a_retry_affordance_on_an_indeterminate_job(client):
    c, store, _ = client
    store.create_scheduled_run(ScheduledRun(
        id="sched-ind", project_path=DEMO, prompt="did it spawn?",
        mode="terminal", scheduled_for=1000,
    ))
    store.claim_specific("sched-ind")
    store.reconcile_launching(2000, store.launching_scheduled_run_ids())

    body = c.get("/").text

    assert 'data-scheduled-retry="sched-ind"' in body


def test_the_scheduled_section_offers_a_retry_affordance_on_a_missed_job(client):
    c, store, _ = client
    store.create_scheduled_run(ScheduledRun(
        id="sched-missed", project_path=DEMO, prompt="did it spawn?",
        mode="terminal", scheduled_for=1000, status="missed",
    ))

    body = c.get("/").text

    assert 'data-scheduled-retry="sched-missed"' in body


def test_a_job_that_has_already_been_retried_offers_no_second_retry_button(client):
    """The server refuses it, so the panel must not render a control whose only
    possible outcome is a 409."""
    c, store, _ = client
    store.create_scheduled_run(ScheduledRun(
        id="sched-done", project_path=DEMO, prompt="x", mode="terminal",
        scheduled_for=1000,
    ))
    store.claim_specific("sched-done")
    store.finish_scheduled_run("sched-done", status="failed", error="boom")
    store.retry_terminal("sched-done", new_id="sched-done-2", now=3000)

    body = c.get("/").text

    assert 'data-scheduled-job="sched-done"' in body
    assert 'data-scheduled-retry="sched-done"' not in body
    assert 'data-scheduled-retry="sched-done-2"' not in body   # still launching


def test_the_retry_button_carries_only_the_id_it_retries(client):
    """The server owns the prompt, mode, model and permission now. Shipping
    them back down as `data-retry-*` attributes plus a hidden textarea was what
    let the panel launch a schedule while forgetting its handoff."""
    c, store, _ = client
    store.create_scheduled_run(ScheduledRun(
        id="sched-lean", project_path=DEMO, prompt="secret prompt",
        mode="terminal", scheduled_for=1000,
    ))
    store.claim_specific("sched-lean")
    store.finish_scheduled_run("sched-lean", status="failed", error="boom")

    body = c.get("/").text

    assert 'data-scheduled-retry="sched-lean"' in body
    assert "data-retry-path" not in body
    assert "data-scheduled-retry-prompt" not in body
    # Named per row rather than a bare "Retry": once the finished history fills
    # up, twenty of these are twenty identical entries in a button list. The
    # row carries the same label so `settleRow` can name the one it builds.
    assert 'aria-label="Retry demo run scheduled for' in body
    assert 'data-scheduled-retry-label="Retry demo run scheduled for' in body


# --- Follow-up 3: retention and pagination ------------------------------------


def _terminal_job(store, jid, completed_at):
    store.create_scheduled_run(ScheduledRun(
        id=jid, project_path=DEMO, prompt="x", mode="terminal",
        scheduled_for=1000, created_at=1000,
    ))
    store.claim_specific(jid)
    store.finish_scheduled_run(jid, status="fired")
    store.conn.execute(
        "UPDATE scheduled_runs SET completed_at=? WHERE id=?", (completed_at, jid))


def test_get_schedule_pages_and_reports_the_total(client):
    c, store, _ = client
    for i in range(5):
        store.create_scheduled_run(ScheduledRun(
            id=f"p{i}", project_path=DEMO, prompt="x", mode="terminal",
            scheduled_for=1000 + i,
        ))

    page = c.get("/api/schedule", params={"limit": 2, "offset": 1})

    assert page.status_code == 200
    assert [j["id"] for j in page.json()] == ["p1", "p2"]
    assert page.headers["x-total-count"] == "5"
    assert len(c.get("/api/schedule").json()) == 5      # unpaged is still everything


def test_get_schedule_refuses_a_nonsense_page(client):
    c, _, _ = client
    assert c.get("/api/schedule", params={"limit": 0}).status_code == 422
    assert c.get("/api/schedule", params={"offset": -1}).status_code == 422


def test_the_dashboard_caps_terminal_rows_and_says_how_many_it_held_back(client):
    c, store, _ = client
    for i in range(DASHBOARD_TERMINAL_SCHEDULES + 3):
        _terminal_job(store, f"t{i:02d}", completed_at=5000 + i)

    body = c.get("/").text

    shown = re.findall(r'data-scheduled-job="(t\d\d)"', body)
    assert len(shown) == DASHBOARD_TERMINAL_SCHEDULES
    # Newest first: the three oldest are the ones held back.
    assert "t00" not in shown and shown[0] == f"t{DASHBOARD_TERMINAL_SCHEDULES + 2:02d}"
    assert "3 older" in body
    # Reachable, not just announced: a note that names data and offers no route
    # to it is worse than no note. And the <ul> points at it, so someone
    # navigating list-to-list meets the caveat rather than stepping over it.
    assert '<a href="/api/schedule">' in body
    assert 'aria-describedby="scheduled-older"' in body


def test_the_dashboard_never_holds_back_an_active_job(client):
    """Terminal rows are history; a pending job is work the panel still owes
    you, and hiding one behind a cap would hide a launch that is going to
    happen."""
    c, store, _ = client
    for i in range(DASHBOARD_TERMINAL_SCHEDULES + 5):
        store.create_scheduled_run(ScheduledRun(
            id=f"a{i:02d}", project_path=DEMO, prompt="x", mode="terminal",
            scheduled_for=9_000_000_000 + i,
        ))

    body = c.get("/").text

    assert len(re.findall(r'data-scheduled-job="a\d\d"', body)) == \
        DASHBOARD_TERMINAL_SCHEDULES + 5
    assert "older" not in body


def test_the_dashboard_says_nothing_about_older_rows_when_none_were_held_back(client):
    c, store, _ = client
    _terminal_job(store, "t0", completed_at=5000)

    assert "older" not in c.get("/").text


# --- Follow-up 4: the row a mutation leaves behind ----------------------------


def test_an_unrenderable_scheduled_for_omits_the_datetime_attribute(client):
    """`datetime="None"` is not a valid machine-readable date, and an invalid
    one is worse than none at all for anything parsing the page."""
    c, store, _ = client
    store.create_scheduled_run(ScheduledRun(
        id="sched-extreme-2", project_path=DEMO, prompt="x", mode="background",
        scheduled_for=999_999_999_999, created_at=1000,
    ))

    body = c.get("/").text

    assert 'data-scheduled-job="sched-extreme-2"' in body
    assert 'datetime="None"' not in body


def test_every_card_has_a_compose_box_that_posts_to_launch_and_schedule(client):
    c, _, pid = client

    body = c.get("/").text

    cid = f"compose-{pid}"
    assert f'data-compose-run="{cid}"' in body
    assert f'data-schedule-toggle="schedule-{cid}"' in body
    assert f'id="{cid}"' in body
    assert "datetime-local" in body


def test_a_queued_handoff_offers_its_own_schedule_affordance(client):
    c, _, pid = client
    c.post("/api/handoff", json={
        "id": "h-sched", "project_path": DEMO, "next_prompt": "next step",
    })

    body = c.get("/").text

    assert 'data-schedule-toggle="schedule-handoff-h-sched"' in body
    assert 'data-schedule-handoff="h-sched"' in body


# --- Phase 7 Task 2: session-meta enrichment on the detail page --------------


def _write_meta(cfg, session_id, **fields):
    d = cfg.session_meta_dir
    d.mkdir(parents=True, exist_ok=True)
    import json as _json
    body = {"session_id": session_id}
    body.update(fields)
    (d / f"{session_id}.json").write_text(_json.dumps(body), encoding="utf-8")


def test_detail_page_shows_session_meta_activity_when_present(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool",
                "session_meta_dir": tmp_path / "meta"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo", title="Worked",
                      ended_at="2026-07-30T10:00:00.000Z", tokens_in=5, tokens_out=5),
        pid)
    _write_meta(cfg, "s1", files_modified=3, lines_added=120, lines_removed=40,
                git_commits=2, git_pushes=1, duration_minutes=45,
                uses_task_agent=True, uses_mcp=True, uses_web_search=True)

    html = TestClient(create_app(store, cfg)).get(f"/project/{pid}").text

    assert "3 files" in html
    assert "+120" in html and "40" in html
    assert "2 commits" in html and "1 push" in html
    assert "45m" in html
    assert "agent" in html and "mcp" in html and "web" in html
    store.close()


def test_detail_page_omits_token_fields_from_meta(tmp_path):
    # Constraint 1: a meta file's token numbers must never reach the page.
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool",
                "session_meta_dir": tmp_path / "meta"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo", title="Worked",
                      ended_at="2026-07-30T10:00:00.000Z", tokens_in=5, tokens_out=5),
        pid)
    _write_meta(cfg, "s1", input_tokens=99999, output_tokens=88888, files_modified=1)

    html = TestClient(create_app(store, cfg)).get(f"/project/{pid}").text

    assert "99999" not in html and "88888" not in html
    store.close()


def test_detail_page_is_unchanged_when_meta_dir_is_empty(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool",
                "session_meta_dir": tmp_path / "meta"})  # never created
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo", title="Worked",
                      ended_at="2026-07-30T10:00:00.000Z", tokens_in=5, tokens_out=5),
        pid)

    r = TestClient(create_app(store, cfg)).get(f"/project/{pid}")

    assert r.status_code == 200
    assert "Worked" in r.text
    store.close()


def test_detail_page_survives_malformed_meta(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool",
                "session_meta_dir": tmp_path / "meta"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo", title="Worked",
                      ended_at="2026-07-30T10:00:00.000Z", tokens_in=5, tokens_out=5),
        pid)
    (tmp_path / "meta").mkdir(parents=True)
    (tmp_path / "meta" / "s1.json").write_text("{broken", encoding="utf-8")

    r = TestClient(create_app(store, cfg)).get(f"/project/{pid}")

    assert r.status_code == 200
    store.close()


def test_detail_page_hides_zero_activity_meta(tmp_path):
    # A meta file that records a pure Q&A session renders no activity.
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool",
                "session_meta_dir": tmp_path / "meta"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo", title="Worked",
                      ended_at="2026-07-30T10:00:00.000Z", tokens_in=5, tokens_out=5),
        pid)
    _write_meta(cfg, "s1")  # session_id only, all facts zero

    html = TestClient(create_app(store, cfg)).get(f"/project/{pid}").text

    assert "files" not in html.split("<table")[-1] or "0 files" not in html
    store.close()
