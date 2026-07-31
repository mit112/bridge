from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bridge import spool
from bridge.api import create_app
from bridge.config import load
from bridge.models import Handoff, SessionRecord
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
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
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
