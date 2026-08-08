"""The fragment contract: what the router is allowed to assume it will receive.

The full-document assertion matters as much as the fragment one. The 156 existing
route tests all render full documents, and they stay valid only while a request
WITHOUT the fragment header is byte-for-byte what it always was.
"""

import re

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
    pid = store.upsert_project("/Users/you/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/you/dev/demo",
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


# The `shell_status` default now renders the global connection/freshness
# readout (`shell_freshness()`), spelled out independently in both base.html and
# _fragment.html (Jinja blocks do not survive an `{% include %}`, the same reason
# the sibling `.shell__body` markup above is duplicated rather than shared). Only
# project.html still overrides it, and it has no fragment mode at all, so it is
# out of scope here. Overview no longer overrides -- it renders the same default
# -- but that default's "Indexed Xm ago" label is not guaranteed byte-identical
# across two separate requests (a minute boundary can fall between them), so
# overview is excluded to keep this test non-flaky. That leaves the four routes
# whose fixtures never record an index run (so their footer is the static "Not
# indexed yet" branch) and where nothing else compares the two copies -- a
# one-sided edit to either would go undetected until a user hit one of them.
SHELL_STATUS_DEFAULT_ROUTES = [(p, a) for p, a in ROUTES if a != "overview"]


@pytest.mark.parametrize("path,active", SHELL_STATUS_DEFAULT_ROUTES)
def test_the_shell_status_matches_the_full_documents_body(c, path, active):
    """base.html and _fragment.html each spell out the .shell-status default.

    Unlike `.shell__body` above, nothing else in the suite compares this
    swap target's two copies -- a drift here would be invisible until a user
    landed on the one route whose fragment (or whose full document) forgot
    the edit. Whitespace is collapsed before comparing: base.html nests this
    div inside `<aside>` at deeper indentation than _fragment.html's top-level
    copy, by design -- that formatting difference is not the drift this test
    guards against, and a naive byte comparison would fail on it every time.
    """
    full = c[0].get(path).text
    frag = c[0].get(path, headers=FRAGMENT).text
    marker = '<div class="shell-status">'

    def normalize(html: str) -> str:
        return re.sub(r"\s+", " ", html[html.index(marker):].split("</div>")[0]).strip()

    assert normalize(full) == normalize(frag)


def test_the_project_detail_route_serves_a_fragment(c):
    """The workspace is a swap target now: with the fragment header it returns
    the swap payload -- no doctype, no sidebar -- so navigating into a project
    (and between its tabs) swaps the content region instead of tearing down the
    persistent shell. The tab/sort/filter links all share the /project/{id}
    path, so a fragment for the base route is a fragment for all of them."""
    client, pid = c
    for path in (f"/project/{pid}", f"/project/{pid}?tab=sessions"):
        body = client.get(path, headers=FRAGMENT).text
        assert "<!doctype html>" not in body.lower()
        assert '<aside class="sidebar"' not in body
        assert 'class="shell__body"' in body
        assert 'content="projects"' in body  # the active nav key for the swap


def test_the_project_detail_full_document_is_unchanged(c):
    """A request WITHOUT the header is still the whole document it always was."""
    client, pid = c
    body = client.get(f"/project/{pid}").text
    assert "<!doctype html>" in body.lower()
    assert '<aside class="sidebar"' in body
    assert "app.css" in body


@pytest.mark.parametrize("path,active", ROUTES)
def test_fragment_response_is_never_cacheable(c, path, active):
    """A fragment and a full document share a URL, differing only by the request
    header. Without `Cache-Control: no-store` a browser (its memory cache in
    particular, which ignores `Vary`) can store the headless fragment under the
    page URL and then serve it as the whole document on a back/forward
    navigation -- rendering the page unstyled until a manual reload. `Vary`
    marks the dependency for well-behaved shared caches; `no-store` is what
    actually guarantees the fragment is never reused as a document.
    """
    resp = c[0].get(path, headers=FRAGMENT)
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers.get("vary") == "X-Bridge-Fragment"
