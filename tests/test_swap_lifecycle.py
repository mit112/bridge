"""Multi-document-lifetime tests: what still works after the document is swapped.

The rest of the suite is structurally blind to this. Every route test renders one
full document and every test in `test_static_js.py` models exactly one document
lifetime, so a swap lifecycle would otherwise ship with no regression net at all.

These run the REAL static files under node against a hand-rolled DOM
(`tests/js/minidom.js`). That DOM is deliberately small: it models element
identity, attribute selectors and event bubbling, which is all the swap contract
turns on. It does NOT model layout, CSS, or true browser event semantics -- see
the module docstring in minidom.js for the exact boundary.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "bridge" / "static"
MINIDOM = Path(__file__).resolve().parent / "js" / "minidom.js"

NODE_CANDIDATES = (
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
    "/usr/bin/node",
)


def _node() -> str | None:
    # `tools/falsify.py` runs pytest with PATH=/usr/bin:/bin, where Homebrew's
    # node is invisible. A bare `shutil.which` therefore SKIPS this module under
    # falsification, pytest exits 0, and every mutation comes back SURVIVED --
    # a skipped test is indistinguishable from a passing one. Same reasoning and
    # same list as tests/test_static_js.py:32.
    found = shutil.which("node")
    if found:
        return found
    return next((p for p in NODE_CANDIDATES if Path(p).exists()), None)


def run_js(body: str, files: list[str], tmp_path) -> dict:
    """Load `files` from static/ in order into a mini-DOM realm, then run `body`."""
    script = tmp_path / "case.js"
    loads = "\n".join(
        f'load({json.dumps(str(STATIC / name))});' for name in files
    )
    script.write_text(
        f'const {{ makeDocument, load, report }} = require({json.dumps(str(MINIDOM))});\n'
        f'{loads}\n{body}\n'
    )
    proc = subprocess.run([_node(), str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


pytestmark = pytest.mark.skipif(_node() is None, reason="node is not installed")


def test_registry_runs_enter_hooks_in_registration_order(tmp_path):
    got = run_js(
        """
        const seen = [];
        window.bridgePage.onEnter(() => seen.push("a"));
        window.bridgePage.onEnter(() => seen.push("b"));
        window.bridgePage.enter();
        report({ seen });
        """,
        ["shell.js"],
        tmp_path,
    )
    assert got["seen"] == ["a", "b"]


def test_one_bad_enter_hook_does_not_abort_the_others(tmp_path):
    got = run_js(
        """
        const seen = [];
        window.bridgePage.onEnter(() => { throw new Error("boom"); });
        window.bridgePage.onEnter(() => seen.push("survived"));
        window.bridgePage.enter();
        report({ seen });
        """,
        ["shell.js"],
        tmp_path,
    )
    assert got["seen"] == ["survived"], (
        "one page's broken enter hook took down every other page's"
    )


def test_shell_js_fires_the_initial_enter_hook_once_on_first_load(tmp_path):
    """The registry only QUEUES hooks -- nothing else performs the FIRST page
    view. router.js (task 9) calls `enter()` only after a swap, so without a
    bootstrap here every onEnter hook registered by an ordinary page load
    would sit in the queue forever, unfired.

    The ORDER below is the whole point, not incidental: `run_js` loads
    shell.js FIRST, exactly as `base.html` does (it is the first `<script
    defer>`), and only after that does this test register an `onEnter` hook --
    exactly the sequence every other static file follows on a real page. A
    version of this test that registered the hook BEFORE shell.js ran could
    not tell a correct implementation from a broken one: a broken shell.js
    that eagerly calls `enter()` during its own evaluation (instead of
    waiting for `DOMContentLoaded`) would find that hook already queued and
    "coincidentally" pass.

    `DOMContentLoaded` fires only once every deferred script (this one
    included) has finished executing -- readiness is already "interactive"
    the whole time a `<script defer>` runs, never "loading" (see minidom.js's
    `makeDocument`) -- so shell.js must listen for it unconditionally rather
    than branching on `readyState`, which is exactly the bug this test
    guards against."""
    got = run_js(
        """
        const seen = [];
        window.bridgePage.onEnter(() => seen.push("entered"));
        document.dispatchEvent({ type: "DOMContentLoaded" });
        report({ seen });
        """,
        ["shell.js"],
        tmp_path,
    )
    assert got["seen"] == ["entered"], (
        "shell.js must fire the first view's onEnter hooks off "
        "DOMContentLoaded -- registered unconditionally, not gated on "
        "readyState, which a deferred script never observes as \"loading\""
    )


def test_scheduled_times_are_repainted_on_every_page_view(tmp_path):
    """schedule.js bound this to DOMContentLoaded, which fires once per document.

    The server stores epoch seconds and only the browser knows the viewer's
    timezone, so a cell that is never repainted shows a raw UTC string forever.
    """
    got = run_js(
        """
        const { El } = require(MINIDOM);
        const cell = new El("span", { "data-scheduled-for": "1754300000" });
        document.body.append(cell);
        const beforeEnter = cell.textContent;
        window.bridgePage.enter();
        const afterFirst = cell.textContent;

        // A second page view with a fresh cell -- what a swap actually produces.
        const swapped = new El("span", { "data-scheduled-for": "1754300000" });
        cell.remove();
        document.body.append(swapped);
        window.bridgePage.enter();
        report({ beforeEnter, afterFirst, afterSwap: swapped.textContent });
        """.replace("MINIDOM", json.dumps(str(MINIDOM))),
        ["shell.js", "schedule.js"],
        tmp_path,
    )
    assert got["beforeEnter"] == ""
    assert got["afterFirst"] != ""
    assert got["afterSwap"] != "", (
        "a scheduled time rendered after a swap was never converted to local "
        "time -- the viewer sees the server's raw UTC value"
    )


def test_leave_hooks_run_on_leave_and_not_on_enter(tmp_path):
    got = run_js(
        """
        const seen = [];
        window.bridgePage.onLeave(() => seen.push("left"));
        window.bridgePage.enter();
        const afterEnter = seen.slice();
        window.bridgePage.leave();
        report({ afterEnter, afterLeave: seen });
        """,
        ["shell.js"],
        tmp_path,
    )
    assert got["afterEnter"] == []
    assert got["afterLeave"] == ["left"]


def test_view_toggle_reannounces_after_a_swap(tmp_path):
    """projects.html always renders List as pressed and relies on JS to correct it.

    Run once at load, the correction never happens on a swapped navigation: the
    layout is grid and both buttons announce "List, pressed" -- permanently.
    """
    got = run_js(
        """
        const { El } = require(MINIDOM);
        document.documentElement.setAttribute("data-projects-view", "grid");
        function freshButtons() {
          document.body.children.length = 0;
          const list = new El("button", { "data-projects-view-button": "list",
                                          "aria-pressed": "true" });
          const grid = new El("button", { "data-projects-view-button": "grid",
                                          "aria-pressed": "false" });
          document.body.append(list); document.body.append(grid);
          return { list, grid };
        }
        const first = freshButtons();
        window.bridgePage.enter();
        const afterFirst = first.grid.getAttribute("aria-pressed");
        const second = freshButtons();
        window.bridgePage.enter();
        report({ afterFirst, afterSwap: second.grid.getAttribute("aria-pressed") });
        """.replace("MINIDOM", json.dumps(str(MINIDOM))),
        ["shell.js", "projects.js"],
        tmp_path,
    )
    assert got["afterFirst"] == "true"
    assert got["afterSwap"] == "true", (
        'after a swap the grid is shown but the toggle still announces '
        '"List, pressed"'
    )


def test_an_edited_prompt_is_saved_before_the_content_is_swapped(tmp_path):
    """Removing a focused node does not fire focusout in any browser.

    A full document navigation fires it, so this worked before the shell
    persisted. Detaching the node does not, and the prompt is the one thing
    Bridge cannot rebuild from transcripts -- so an unflushed edit is
    unrecoverable data loss, not a cosmetic regression.
    """
    got = run_js(
        """
        const { El } = require(MINIDOM);
        const field = new El("textarea", { "data-prompt-handoff": "7", id: "p7" });
        field.defaultValue = "ORIGINAL";
        field.value = "EDITED BY THE USER";
        document.body.append(field);
        window.bridgePage.leave();
        report({ calls: globalThis.__calls.fetch });
        """.replace("MINIDOM", json.dumps(str(MINIDOM))),
        ["shell.js", "copy.js", "launch.js"],
        tmp_path,
    )
    patches = [c for c in got["calls"] if c["opts"]["method"] == "PATCH"]
    assert len(patches) == 1, (
        "leaving the page discarded an edited prompt with no PATCH -- the user's "
        "text is gone and nothing told them"
    )
    assert "EDITED BY THE USER" in patches[0]["opts"]["body"]


def test_an_unchanged_prompt_is_not_patched_on_leave(tmp_path):
    """A PATCH per navigation would re-journal an unchanged prompt every time."""
    got = run_js(
        """
        const { El } = require(MINIDOM);
        const field = new El("textarea", { "data-prompt-handoff": "7", id: "p7" });
        field.defaultValue = "ORIGINAL";
        field.value = "ORIGINAL";
        document.body.append(field);
        window.bridgePage.leave();
        report({ calls: globalThis.__calls.fetch });
        """.replace("MINIDOM", json.dumps(str(MINIDOM))),
        ["shell.js", "copy.js", "launch.js"],
        tmp_path,
    )
    assert got["calls"] == []


def test_a_failed_leave_flush_still_surfaces_its_warning_before_the_swap(tmp_path):
    """Carried finding from Task 6, resolved here (not deferred further).

    router.js's navigate() used to call bridgePage.leave() and move straight on
    to fetching the fragment and replacing .shell__body -- fire-and-forget, per
    Task 6's "don't await inside the hook" mandate. A PATCH that settled later
    than the fragment fetch landed AFTER the status node it warns through had
    already been swapped away, so announce()'s `if (status)` guard turned the
    "Not saved" warning into a silent no-op: the user lost both the edit and
    the warning.

    bridgePage.leave() now returns a promise that resolves only once every leave
    hook's own async work has settled, and router.js awaits it before fetching
    the fragment (see the companion static check in test_shell_contract.py).
    This proves the mechanism directly, independent of the router: a PATCH
    rigged to settle several microtask ticks later than a bare, un-awaited
    leave() call ever waited for still lands its announce() on a live node
    before anything simulating a swap removes it.
    """
    got = run_js(
        """
        const { El } = require(MINIDOM);
        const field = new El("textarea", { "data-prompt-handoff": "7", id: "p7" });
        field.defaultValue = "ORIGINAL";
        field.value = "EDITED BY THE USER";
        document.body.append(field);
        const status = new El("span", { "data-prompt-status": "p7" });
        document.body.append(status);

        // The handoff PATCH settles several microtask ticks later than an
        // un-awaited leave() call would ever wait for -- exactly the race the
        // carried finding describes ("resolves after the swap").
        globalThis.fetch = (url, opts) => {
          globalThis.__calls.fetch.push({ url, opts });
          let p = Promise.resolve();
          for (let i = 0; i < 4; i += 1) p = p.then(() => {});
          return p.then(() => { throw new Error("network down"); });
        };

        (async () => {
          const result = window.bridgePage.leave();
          await result;
          // Simulate the swap discarding the node AFTER leave() has settled --
          // exactly what router.js's navigate() now does.
          status.remove();
          report({
            leaveIsAwaitable: !!(result && typeof result.then === "function"),
            text: status.textContent,
          });
        })();
        """.replace("MINIDOM", json.dumps(str(MINIDOM))),
        ["shell.js", "copy.js", "launch.js"],
        tmp_path,
    )
    assert got["leaveIsAwaitable"], (
        "bridgePage.leave() must return a promise, or router.js has nothing to "
        "await before it fetches the fragment and swaps"
    )
    assert "Not saved" in got["text"], (
        "the leave flush's warning never landed before something (a swap) "
        "removed the node it announces through -- it vanished silently"
    )


def test_router_exposes_navigate_for_in_app_redirects(tmp_path):
    got = run_js(
        'report({ has: typeof window.bridgeNavigate });',
        ["shell.js", "router.js"],
        tmp_path,
    )
    assert got["has"] == "function"


def test_router_ignores_a_modified_click_on_a_swappable_link(tmp_path):
    """The standard opt-out: cmd/ctrl/shift/alt-click must keep its browser
    meaning (new tab, new window, save-as) rather than being intercepted.

    Mutation-verify guard for Step 7's first mutation (drop the modifier-key
    guard) -- without it, this is the test that fails.
    """
    got = run_js(
        """
        const { El } = require(MINIDOM);
        const link = new El("a", { href: "/projects" });
        document.body.append(link);
        let prevented = false;
        const event = {
          type: "click", target: link, button: 0,
          metaKey: true, ctrlKey: false, shiftKey: false, altKey: false,
          defaultPrevented: false,
          preventDefault() { prevented = true; },
        };
        link.dispatchEvent(event);
        report({ prevented, fetches: globalThis.__calls.fetch.length });
        """.replace("MINIDOM", json.dumps(str(MINIDOM))),
        ["shell.js", "router.js"],
        tmp_path,
    )
    assert got["prevented"] is False, (
        "a cmd-click on a swappable link was intercepted -- this breaks "
        "cmd-click-to-new-tab"
    )
    assert got["fetches"] == 0


def test_router_swaps_a_navigation_into_the_project_workspace(tmp_path):
    """/project/{id} is a swap target now, so bridgeNavigate() to a workspace URL
    (the "Open project" link, and by the shared path every in-project
    tab/sort/filter/pager link) fetches the fragment instead of doing a full
    load -- that full document load is the shell-teardown flash this removes. A
    NON-swappable path would call location.assign() straight away and never
    fetch, so a single fetch carrying the fragment header proves the workspace
    path went through the swap route. Mirrors the failed-fetch test's shape
    (ok:false so it never reaches the fragment parse)."""
    got = run_js(
        """
        globalThis.fetch = (url, opts) => {
          globalThis.__calls.fetch.push({ url, opts });
          return Promise.resolve({ ok: false, status: 500 });
        };
        (async () => {
          await window.bridgeNavigate("/project/7?tab=sessions");
          report({
            fetches: globalThis.__calls.fetch.length,
            header: globalThis.__calls.fetch[0]
              ? globalThis.__calls.fetch[0].opts.headers["X-Bridge-Fragment"] : null,
          });
        })();
        """,
        ["shell.js", "router.js"],
        tmp_path,
    )
    assert got["fetches"] == 1, (
        "bridgeNavigate to a /project/{id} URL did not fetch a fragment -- the "
        "workspace path is not being treated as swappable, so it stays a full "
        "load (the shell-teardown flash)"
    )
    assert got["header"] == "1", "the swap fetch must ask for the fragment payload"


def test_router_lets_the_browser_handle_a_same_document_hash_link(tmp_path):
    """The skip-link (base.html:101, `<a class="skip-link" href="#main">`,
    present on every page) is an in-page focus jump, not a navigation.

    `new URL("#main", window.location.href).pathname` resolves to the CURRENT
    path, so `swappable()` alone can't tell "#main" apart from a real
    same-path navigation -- without a dedicated guard, the click delegate
    intercepts it, re-fetches the fragment, and swaps the page out from under
    the user instead of letting the browser move focus. On a fetch failure the
    catch fallback would then do a FULL PAGE RELOAD for "Skip to content".
    """
    got = run_js(
        """
        const { El } = require(MINIDOM);
        const link = new El("a", { href: "#main", class: "skip-link" });
        document.body.append(link);
        let prevented = false;
        const event = {
          type: "click", target: link, button: 0,
          metaKey: false, ctrlKey: false, shiftKey: false, altKey: false,
          defaultPrevented: false,
          preventDefault() { prevented = true; },
        };
        link.dispatchEvent(event);
        report({ prevented, fetches: globalThis.__calls.fetch.length });
        """.replace("MINIDOM", json.dumps(str(MINIDOM))),
        ["shell.js", "router.js"],
        tmp_path,
    )
    assert got["prevented"] is False, (
        "the skip-link's #main jump was intercepted by the router -- this "
        "turns \"Skip to content\" into a fragment swap (or, on a fetch "
        "failure, a full page reload)"
    )
    assert got["fetches"] == 0


def test_router_falls_back_to_a_real_navigation_on_a_failed_fetch(tmp_path):
    """Mutation-verify guard for Step 7's second mutation (drop the `catch`
    fallback).

    `location.assign` also appears in navigate()'s own swappable-guard (a
    distinct code path from `window.bridgeNavigate`, since that function is
    exposed to any caller with any href), so a bare source substring check
    cannot tell "the catch's fallback is intact" from "only the guard's
    fallback survived." This drives an actual failing fetch through
    navigate() with a URL that passes the swappable guard, so only the
    catch's own fallback can be responsible for the assign.
    """
    got = run_js(
        """
        globalThis.fetch = (url, opts) => {
          globalThis.__calls.fetch.push({ url, opts });
          return Promise.resolve({ ok: false, status: 500 });
        };
        (async () => {
          await window.bridgeNavigate("/projects");
          report({ assigned: globalThis.__calls.locationAssign });
        })();
        """,
        ["shell.js", "router.js"],
        tmp_path,
    )
    assert got["assigned"] == "/projects", (
        "a server error on the fragment fetch must fall back to a real "
        "navigation, or the user is stranded on a link that did nothing"
    )


def test_only_one_event_source_across_many_navigations(tmp_path):
    """The surviving SSE connection is the concrete win of the persistent shell.

    live.js opens EventSource("/events") per document today. If a page view ever
    re-opened it, the panel would fan out N connections per tab instead of one.
    """
    got = run_js(
        """
        window.bridgePage.enter();
        window.bridgePage.leave();
        window.bridgePage.enter();
        window.bridgePage.leave();
        window.bridgePage.enter();
        report({ sources: globalThis.__calls.eventSource.length,
                 intervals: globalThis.__calls.interval });
        """,
        ["shell.js", "live.js"],
        tmp_path,
    )
    assert got["sources"] == 1, f"{got['sources']} SSE connections for one tab"
    assert got["intervals"] == 1, f"{got['intervals']} age tickers running at once"


def test_the_freshness_strip_is_reseeded_after_returning_to_overview(tmp_path):
    """announceConnectionState early-returns when the state matches its cache.

    The SSE stream keeps delivering while the user is on a page with no strip, so
    lastConnectionState drifts on while nothing is written. Coming back inserts a
    fresh server-rendered strip, and the cached value then suppresses the very
    write that would correct it -- the strip freezes at whatever the server
    happened to render at swap time.
    """
    got = run_js(
        """
        const { El } = require(MINIDOM);
        function strip(server) {
          document.body.children.length = 0;
          const s = new El("section", { "data-freshness-strip": "",
                                        "data-index-at": "1754300000",
                                        "data-server": server });
          const label = new El("span", { "data-freshness-label": "" });
          s.append(label); document.body.append(s);
          return { s, label };
        }
        const first = strip("available");
        window.bridgePage.enter();
        const afterFirst = first.s.getAttribute("data-freshness-state");
        // Away to a page with no strip, then back to a freshly rendered one.
        window.bridgePage.leave();
        document.body.children.length = 0;
        window.bridgePage.enter();
        const back = strip("available");
        window.bridgePage.enter();
        report({ afterFirst, afterReturn: back.s.getAttribute("data-freshness-state"),
                 label: back.label.textContent });
        """.replace("MINIDOM", json.dumps(str(MINIDOM))),
        ["shell.js", "live.js"],
        tmp_path,
    )
    assert got["afterFirst"], "the strip was never seeded on the first page view"
    assert got["afterReturn"] == got["afterFirst"], (
        "the strip inserted by a swap was never written to -- announceConnectionState's "
        "cached state suppressed the correction"
    )
    assert got["label"] != ""


def test_five_navigations_leave_exactly_one_of_everything(tmp_path):
    """The whole point, asserted once: N page views, one connection, one ticker.

    Every duplicate hazard in this app is a doubled delegated listener -- two
    POST /api/launch is two spawned terminal sessions, two POST /api/schedule is
    two scheduled rows. Counting listeners is what catches all of them at once.
    """
    got = run_js(
        """
        for (let i = 0; i < 5; i += 1) {
          window.bridgePage.enter();
          window.bridgePage.leave();
        }
        report({
          sources: globalThis.__calls.eventSource.length,
          intervals: globalThis.__calls.interval,
          click: document.listenerCount("click"),
          focusout: document.listenerCount("focusout"),
          change: document.listenerCount("change"),
          input: document.listenerCount("input"),
        });
        """,
        ["shell.js", "router.js", "copy.js", "launch.js", "schedule.js",
         "live.js", "projects.js", "settings.js"],
        tmp_path,
    )
    assert got["sources"] == 1
    assert got["intervals"] == 1
    # Each file registers its own delegated click at load; the invariant is that
    # the count does NOT grow with the number of page views.
    baseline = got["click"]
    assert baseline < 10, f"{baseline} click listeners suggests re-registration"
    for key in ("focusout", "change", "input"):
        assert got[key] <= 2, f"{key} listeners multiplied across page views"
