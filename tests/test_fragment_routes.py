"""The fragment contract: what the router is allowed to assume it will receive.

The full-document assertion matters as much as the fragment one. The 156 existing
route tests all render full documents, and they stay valid only while a request
WITHOUT the fragment header is byte-for-byte what it always was.
"""

import pytest
from fastapi.testclient import TestClient

from bridge.api import create_app
from bridge.config import load
from bridge.models import SessionRecord
from bridge.store import Store

ROUTES = [("/", "overview"), ("/projects", "projects"), ("/schedule", "schedule"),
          ("/diagnostics", "diagnostics"), ("/settings", "settings")]
FRAGMENT = {"X-Bridge-Fragment": "1"}


# There is NO global `client` fixture in conftest.py -- `tests/test_api.py:19`
# defines a local one that yields a 3-tuple `(TestClient, store, pid)`. This
# module needs only the client, so it builds its own rather than importing a
# fixture whose shape it does not use. The autouse guards in conftest.py that
# keep tests off the real Bridge directory apply here regardless.
@pytest.fixture
def c(tmp_path):
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
    yield TestClient(create_app(store, cfg)), pid
    store.close()


@pytest.mark.parametrize("path,active", ROUTES)
def test_fragment_carries_every_swap_target(c, path, active):
    body = c[0].get(path, headers=FRAGMENT).text
    assert "<title>" in body
    assert 'class="shell__body"' in body
    assert 'class="shell-status"' in body
    assert f'content="{active}"' in body


@pytest.mark.parametrize("path,active", ROUTES)
def test_fragment_is_not_a_whole_document(c, path, active):
    body = c[0].get(path, headers=FRAGMENT).text
    assert "<!doctype html>" not in body.lower()
    # Matched on the sidebar's own class, not a bare "<aside" -- overview.html's
    # content legitimately has its own semantic `<aside class="overview-panel
    # overview-up-next-panel">` (the "Up next" panel), so a bare substring
    # check would fail on that route regardless of whether the fragment
    # carries the sidebar.
    assert '<aside class="sidebar"' not in body, (
        "the fragment carries the sidebar, so a swap would replace the very "
        "chrome this design exists to keep"
    )
    assert "app.css" not in body


@pytest.mark.parametrize("path,active", ROUTES)
def test_a_request_without_the_header_is_unchanged(c, path, active):
    body = c[0].get(path).text
    assert "<!doctype html>" in body.lower()
    assert '<aside class="sidebar"' in body
    assert "app.css" in body


@pytest.mark.parametrize("path,active", ROUTES)
def test_the_fragment_body_matches_the_full_documents_body(c, path, active):
    """base.html and _fragment.html each spell out the .shell__body markup.

    Jinja blocks do not survive an `{% include %}`, so the two layouts cannot
    share that markup by factoring. This asserts they never drift instead --
    which is the real risk, because drift would be invisible until a user hit
    the one page whose header the fragment forgot.
    """
    full = c[0].get(path).text
    frag = c[0].get(path, headers=FRAGMENT).text
    marker = '<div class="shell__body">'
    assert full[full.index(marker):].split("</div>")[0][:400] \
        == frag[frag.index(marker):].split("</div>")[0][:400]


def test_the_project_detail_route_has_no_fragment_mode(c):
    """Out of scope by design: the router never intercepts a link to it."""
    client, pid = c
    body = client.get(f"/project/{pid}", headers=FRAGMENT).text
    assert "<!doctype html>" in body.lower()
