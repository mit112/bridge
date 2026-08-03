"""The shared app shell: skip link, landmarks, and the sidebar nav.

Every page now extends the same `base.html` shell (sidebar + page-head +
`<main>`), so these properties -- one `<h1>`, a skip link ahead of the nav, a
labelled `<nav>` landmark, the active item's `aria-current` -- are asserted
once here rather than re-derived per page. Nav destinations are added by the
milestone that lands their route: Overview/Diagnostics shipped first,
Projects followed (Task 2.5), Schedule followed that (Task 4.2), and Settings
(Task 5.2) completes the set -- every entry the sidebar renders now has a
working route behind it.
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from bridge.api import create_app
from bridge.config import load
from bridge.store import Store


def _client(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool"})
    return TestClient(create_app(Store(cfg.db_path), cfg))


def test_skip_link_is_first_focusable(tmp_path):
    html = _client(tmp_path).get("/").text
    assert html.index('href="#main"') < html.index("<nav")
    assert 'id="main"' in html


def test_one_h1_and_landmarks(tmp_path):
    html = _client(tmp_path).get("/").text
    assert len(re.findall(r"<h1\b", html)) == 1
    assert '<nav aria-label="Primary"' in html
    assert '<main id="main"' in html


def test_active_nav_marks_aria_current(tmp_path):
    html = _client(tmp_path).get("/").text
    assert re.search(r'href="/"[^>]*aria-current="page"', html) or \
        re.search(r'aria-current="page"[^>]*href="/"', html)


def test_nav_has_no_dead_ends(tmp_path):
    html = _client(tmp_path).get("/").text
    # Every nav entry the shell renders now has a real route behind it.
    assert 'href="/projects"' in html
    assert 'href="/schedule"' in html
    assert 'href="/settings"' in html
    assert 'href="/diagnostics"' in html


def test_diagnostics_nav_item_is_marked_active_on_its_own_page(tmp_path):
    html = _client(tmp_path).get("/diagnostics").text
    assert re.search(r'href="/diagnostics"[^>]*aria-current="page"', html) or \
        re.search(r'aria-current="page"[^>]*href="/diagnostics"', html)


def test_schedule_nav_item_is_marked_active_on_its_own_page(tmp_path):
    html = _client(tmp_path).get("/schedule").text
    assert re.search(r'href="/schedule"[^>]*aria-current="page"', html) or \
        re.search(r'aria-current="page"[^>]*href="/schedule"', html)


def test_settings_nav_item_is_marked_active_on_its_own_page(tmp_path):
    html = _client(tmp_path).get("/settings").text
    assert re.search(r'href="/settings"[^>]*aria-current="page"', html) or \
        re.search(r'aria-current="page"[^>]*href="/settings"', html)


def test_every_shell_page_has_exactly_one_h1_and_the_shared_landmarks(tmp_path):
    c = _client(tmp_path)
    cfg = load({"db_path": tmp_path / "b.db", "spool_dir": tmp_path / "spool2"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.close()
    client = TestClient(create_app(Store(cfg.db_path), cfg))
    for path in ("/", "/diagnostics", "/projects", f"/project/{pid}", "/schedule"):
        html = client.get(path).text
        assert len(re.findall(r"<h1\b", html)) == 1, path
        assert '<nav aria-label="Primary"' in html, path
        assert '<main id="main"' in html, path
        assert html.index('href="#main"') < html.index("<nav"), path


def test_stylesheet_defines_interaction_states():
    """Task 1.4: buttons get a full default/hover/pressed/focus/disabled/
    loading state set, and a visible focus ring exists globally."""
    css = (
        Path(__file__).resolve().parent.parent
        / "src" / "bridge" / "static" / "app.css"
    ).read_text()
    for sel in (
        ".btn:hover", ".btn:active", ".btn:focus-visible", ".btn:disabled",
        '.btn[aria-busy="true"]', ":focus-visible",
    ):
        assert sel in css
    assert "prefers-reduced-motion" in css
