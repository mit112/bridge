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


def test_shell_renders_structural_bridge_mark_and_status_slot(tmp_path):
    """Removing Bridge's only product-specific shell signature must fail.

    The mark is decorative because the adjacent wordmark already supplies the
    accessible name; the status slot carries its own visible words.
    """
    html = _client(tmp_path).get("/").text
    assert 'class="bridge-mark"' in html
    assert 'class="shell-status"' in html
    assert re.search(r'<svg[^>]*class="bridge-mark"[^>]*aria-hidden="true"', html)


def test_overview_shell_footer_uses_real_connection_and_index_freshness(tmp_path):
    cfg = load({"db_path": tmp_path / "fresh.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    store.record_index_run(
        {"files_seen": 1, "files_scanned": 1, "lines_parsed": 1,
         "sessions_upserted": 1, "parse_errors": 0},
        ran_at=1,
        duration_ms=1,
    )
    html = TestClient(create_app(store, cfg)).get("/").text
    footer = html[html.index('class="shell-status"'):]
    footer = footer[:footer.index("</div>")]
    assert "Connected" in footer
    assert "Indexed" in footer
    assert "Local control plane" not in footer
    store.close()


def test_connection_freshness_status_is_global_across_every_shell_page(tmp_path):
    """#7: the sidebar connection/freshness readout is shell chrome, so it has
    to appear on every page -- not only Overview and the project detail. A page
    still showing the static "Local control plane" placeholder would be a page
    the global status forgot. Same index run, so every page reads the same
    Connected/Indexed footer Overview already renders.
    """
    cfg = load({"db_path": tmp_path / "global.db", "spool_dir": tmp_path / "spool-global"})
    store = Store(cfg.db_path)
    store.record_index_run(
        {"files_seen": 1, "files_scanned": 1, "lines_parsed": 1,
         "sessions_upserted": 1, "parse_errors": 0},
        ran_at=1,
        duration_ms=1,
    )
    client = TestClient(create_app(store, cfg))
    for path in ("/projects", "/schedule", "/settings", "/diagnostics"):
        html = client.get(path).text
        footer = html[html.index('class="shell-status"'):]
        footer = footer[:footer.index("</div>")]
        assert "Connected" in footer, path
        assert "Indexed" in footer, path
        assert "Local control plane" not in footer, path
    store.close()


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
    for path in (
        "/", "/diagnostics", "/projects", f"/project/{pid}", "/schedule", "/settings",
    ):
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


def test_visual_foundation_uses_approved_control_geometry():
    """A regression to the cramped 32px/4px pre-fidelity scale must fail."""
    css = _app_css()
    assert re.search(r"--control-min:\s*2\.5rem", css)
    assert re.search(r"--radius-sm:\s*6px", css)
    button_rule = re.search(r"\.btn\s*\{([^}]*)\}", css, re.DOTALL)
    assert button_rule
    assert "display: inline-flex" in button_rule.group(1)
    assert "text-decoration: none" in button_rule.group(1)


def _app_css():
    return (
        Path(__file__).resolve().parent.parent
        / "src" / "bridge" / "static" / "app.css"
    ).read_text()


def test_responsive_layers_cover_every_breakpoint_with_no_dead_zone():
    """Task 5.4: large sidebar at >=1024px, Menu disclosure below it, a
    single-column phone layer below 768px, and a tablet layer bridging
    768-1023px so nothing in that range is stuck with desktop spacing behind
    a collapsed sidebar (the dead zone the recon flagged)."""
    css = _app_css()
    assert "@media (min-width: 1024px)" in css
    assert "@media (max-width: 1023px)" in css
    assert "@media (max-width: 767px)" in css
    assert "@media (min-width: 768px) and (max-width: 1023px)" in css
    # main stays bounded-width regardless of viewport.
    assert "width: min(100%, 74rem)" in css


def test_narrow_touch_targets_reach_44px():
    """Task 5.4 (folding in the 5.2 review's --control-min/.launch__select
    gap): every control sized off the shared --control-min token -- the
    menu-toggle, .btn, .launch__select, form inputs/selects -- must clear the
    44px touch-target floor at narrow widths, not just .sidebar__link."""
    css = _app_css()
    assert "min-height: 44px" in css or "2.75rem" in css
    narrow = re.search(
        r"@media \(max-width: 1023px\)\s*\{\s*:root\s*\{[^}]*--control-min:\s*2\.75rem",
        css,
    )
    assert narrow, "expected --control-min to be raised to 2.75rem (44px) under max-width: 1023px"


def test_project_tabs_tighten_before_the_320px_reflow_boundary():
    """Four full tab labels must fit the 288px content box at 320px."""
    css = _app_css()
    narrow = re.search(
        r"@media \(max-width: 359px\)\s*\{[^}]*"
        r"\.workspace-tabs\s*\{[^}]*gap:\s*var\(--space-3\)",
        css,
        re.DOTALL,
    )
    assert narrow, "expected narrow workspace tabs to use the 12px gap"


def test_sidebar_toggle_renders_on_every_shell_page_with_a_name_and_a_target(tmp_path):
    """The collapse control is shell chrome, so it must exist on every page --
    collapsing on Overview and navigating to Projects with no way back would
    strand the nav.

    Glyph-only, so the accessible name is the ONLY name: the `<svg>` is
    aria-hidden and there is no text node. Asserted per-page rather than once
    because base.html is shared but nothing else would notice a page that
    overrode the `sidebar__head` block.
    """
    c = _client(tmp_path)
    cfg = load({"db_path": tmp_path / "tog.db", "spool_dir": tmp_path / "spool-tog"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.close()
    client = TestClient(create_app(Store(cfg.db_path), cfg))
    for path in (
        "/", "/diagnostics", "/projects", f"/project/{pid}", "/schedule", "/settings",
    ):
        html = client.get(path).text
        button = re.search(r"<button[^>]*data-sidebar-toggle[^>]*>", html)
        assert button, f"{path}: no sidebar toggle"
        markup = button.group(0)
        assert 'aria-label="Collapse sidebar"' in markup, f"{path}: toggle has no name"
        assert 'aria-expanded="true"' in markup, f"{path}: toggle has no state"
        assert 'aria-controls="primary-nav"' in markup, f"{path}: toggle controls nothing"
        # The thing it claims to control has to be the thing that exists.
        assert 'id="primary-nav"' in html, path
    assert c  # the fixture client is exercised by the other tests in this module


def test_collapsed_rail_hides_nav_rather_than_reducing_it_to_bare_icons():
    """A collapsed rail must not become an icon-only destination list --
    unlabelled icon navigation is mystery meat, and the toggle has to stay
    focusable there or the collapsed rail has no way out.

    Pinned in CSS because that is where the decision lives: `.sidebar__nav`
    going `display: none` (rather than its labels being hidden) is what makes
    the icon-rail variant impossible to reach by accident.
    """
    css = _app_css()
    collapsed = re.search(
        r':root\[data-nav="collapsed"\][^{]*\.sidebar__nav[^{]*\{[^}]*display:\s*none',
        css,
        re.DOTALL,
    )
    assert collapsed, "expected the collapsed rail to hide .sidebar__nav outright"
    assert ".sidebar-toggle:focus-visible" in css
    # The two disclosures must never both be exposed: inverse breakpoints.
    assert re.search(
        r"@media \(min-width: 1024px\) \{ \.sidebar-toggle \{ display: inline-flex",
        css,
    ), "expected .sidebar-toggle to appear only at >=1024px"


def test_desktop_rail_is_fixed_and_the_body_scrolls_instead_of_the_document():
    """The nav holds a finite set of destinations and must not scroll away with
    the page: at >=1024px the shell is viewport-height and `.shell__body` owns
    the scrolling.

    Two things this deliberately does NOT do, both asserted so a later edit
    cannot quietly undo the reasoning: the cage is scoped to >=1024px (below it
    the sidebar stacks above the content, where a 100vh cage would strand
    everything under it), and `.sidebar` keeps its own overflow so the nav is
    reachable at a short viewport or high zoom rather than clipped.
    """
    css = _app_css()
    desktop = re.search(
        r"@media \(min-width: 1024px\) \{(.*?)\n\}", css, re.DOTALL,
    )
    assert desktop, "expected a min-width: 1024px block"
    block = desktop.group(1)
    assert "height: 100vh" in block
    assert "overflow: hidden" in block
    assert re.search(r"\.shell__body \{ overflow-y: auto", block)
    assert re.search(r"\.sidebar \{ overflow-y: auto", block)
    # 100dvh would be dropped by a browser without dvh support, leaving
    # overflow:hidden over a min-height:100vh grid -- content clipped, no scroll.
    assert "100dvh" not in block


def test_reduced_motion_disables_btn_transitions():
    """Task 5.4: prefers-reduced-motion only covered scroll-behavior and the
    skip-link before this; extend it to the .btn transitions too.

    The sheet now has MORE than one reduced-motion block -- the view transition
    keeps its own, next to the rules it switches off, rather than having those
    rules live a thousand lines from everything they relate to. So this reads
    every such block and asserts the button/shell rules appear in one of them.
    The original single-block regex silently described "whichever block comes
    first in the file", which stopped being the intended one the moment another
    was added; the invariant was always "reduced motion reaches these rules".
    """
    css = _app_css()
    blocks = re.findall(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}",
        css,
        re.DOTALL,
    )
    assert blocks, "expected a prefers-reduced-motion block"
    assert any(".btn" in b and "transition: none" in b for b in blocks), (
        "no prefers-reduced-motion block disables the .btn transitions"
    )
    # The sidebar collapse animates grid-template-columns; under reduced motion
    # it must snap instead.
    assert any(".shell { transition: none; }" in b for b in blocks), (
        "no prefers-reduced-motion block snaps the sidebar collapse"
    )
