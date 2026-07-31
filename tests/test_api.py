import pytest
from fastapi.testclient import TestClient

from bridge.api import create_app
from bridge.config import load
from bridge.models import SessionRecord
from bridge.store import Store


@pytest.fixture
def client(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db"})
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
    cfg = load({"db_path": tmp_path / "empty.db"})
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

    cfg = load({"db_path": tmp_path / "s.db", "stale_hours": 1})
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

    cfg = load({"db_path": tmp_path / "n.db"})
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

    cfg = load({"db_path": tmp_path / "conc.db"})
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

    cfg = load({"db_path": tmp_path / "mixed.db"})
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
