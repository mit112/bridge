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
