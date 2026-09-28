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



# A node harness that hangs must fail the run, not hang it. These finish in
# well under a second; the cap only ever fires on a genuinely wedged process.
NODE_TIMEOUT_S = 60

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
    proc = subprocess.run([_node(), str(script)], capture_output=True, text=True, timeout=NODE_TIMEOUT_S)
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
          document.body.childNodes = [];
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


# --- Codex review finding #13: out-of-order navigation completion -----------
#
# navigate() had no cancellation token or generation guard: two rapid
# navigations (a fast double-click, or a click racing a popstate) can have
# their fetches resolve in EITHER order, and nothing stopped the OLDER one
# from applying its (now stale) fragment and URL last, after the newer
# navigation had already finished. `parseFragment`/`applyFragment` are
# top-level `function` declarations in router.js -- `load()` runs the file
# via `vm.runInThisContext` in script mode, so reassigning them on
# `globalThis` after load redirects the bare-identifier calls navigate()
# itself makes, letting this isolate the epoch guard from DOMParser/
# `replaceWith`, neither of which minidom models.
def test_an_older_navigation_resolving_last_does_not_win(tmp_path):
    got = run_js(
        """
        const applied = [];
        const pending = [];
        globalThis.fetch = (url) => new Promise((resolve) => {
          pending.push({ url, resolve });
        });
        globalThis.parseFragment = (html) => ({ marker: html });
        globalThis.applyFragment = (parsed) => { applied.push(parsed.marker); return true; };
        globalThis.window.scrollTo = () => {};   // not modeled by minidom; announceArrival() calls it
        const pushed = [];
        globalThis.history = { pushState(state, title, url) { pushed.push(url); } };

        function tick() { return new Promise((resolve) => setImmediate(resolve)); }

        (async () => {
          // Issued one at a time, each given a tick to reach fetch() before
          // the next starts -- otherwise the SECOND call's epoch bump would
          // supersede the first before it ever fetches at all, which is a
          // different (also-handled) case, not the one under test: two
          // fetches genuinely in flight together, resolving out of order.
          const first = window.bridgeNavigate("/projects");
          await tick();
          const second = window.bridgeNavigate("/schedule");
          await tick();
          // The NEWER navigation's fetch resolves FIRST.
          pending[1].resolve({ ok: true, status: 200, text: async () => "second" });
          await second;
          // The OLDER navigation's fetch resolves LAST -- proving completion
          // order, not issue order, is what the guard keys on.
          pending[0].resolve({ ok: true, status: 200, text: async () => "first" });
          await first;
          report({ applied, pushed });
        })();
        """,
        ["shell.js", "router.js"],
        tmp_path,
    )
    assert got["applied"] == ["second"], (
        "the older, later-resolving navigation applied its stale fragment "
        "over the newer one's already-applied result"
    )
    assert got["pushed"] == ["http://localhost/schedule"], (
        "the older navigation pushed its own (stale) URL into history after "
        "the newer navigation had already navigated there"
    )


def test_a_superseded_navigations_own_failure_does_not_trigger_a_reload(tmp_path):
    """The older navigation's fetch, once superseded, must not fall back to
    `location.assign` on its own error either -- the newer navigation is
    already doing the right thing, and reloading to the OLDER href would
    yank the user back to a page they already navigated away from."""
    got = run_js(
        """
        const pending = [];
        globalThis.fetch = (url) => new Promise((resolve, reject) => {
          pending.push({ url, resolve, reject });
        });
        globalThis.parseFragment = (html) => ({ marker: html });
        globalThis.applyFragment = () => true;
        globalThis.window.scrollTo = () => {};   // not modeled by minidom; announceArrival() calls it
        globalThis.history = { pushState() {} };
        function tick() { return new Promise((resolve) => setImmediate(resolve)); }

        (async () => {
          const first = window.bridgeNavigate("/projects");
          await tick();
          const second = window.bridgeNavigate("/schedule");
          await tick();
          pending[1].resolve({ ok: true, status: 200, text: async () => "second" });
          await second;
          pending[0].reject(new Error("simulated network failure"));
          await first;
          report({ assigned: globalThis.__calls.locationAssign ?? null });
        })();
        """,
        ["shell.js", "router.js"],
        tmp_path,
    )
    assert got["assigned"] is None, (
        "a superseded navigation's own failure triggered a full-page fallback "
        "reload to its stale href"
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
          document.body.childNodes = [];
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
        document.body.childNodes = [];
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


# --- Back must land where the user left, not at the top --------------------
#
# `navigate()` calls `announceArrival()`, which reset both scroll containers to
# 0 unconditionally -- including on a popstate. Every Back therefore dropped
# the user at the top of a page they had scrolled deep into. The position is
# saved on the departing history entry at push time and read back on the pop.
#
# minidom models no layout and no session history stack: `scrollTop` here is a
# plain property and the pop is dispatched by hand with the state a browser
# would restore. What this proves is the bookkeeping -- what is written to the
# entry, and what is applied on the way back.
def test_back_restores_the_scroll_position_the_page_was_left_at(tmp_path):
    got = run_js(
        """
        globalThis.window.scrollTo = () => {};   // not modeled by minidom
        globalThis.fetch = () => Promise.resolve(
          { ok: true, status: 200, text: async () => "FRAGMENT" });
        const body = document.createElement("div");
        body.setAttribute("class", "shell__body");
        document.body.append(body);

        globalThis.parseFragment = (html) => ({ marker: html });
        globalThis.applyFragment = () => true;   // keep the same .shell__body node

        const entries = [];
        globalThis.history = {
          state: null,
          pushState(state, title, url) { entries.push({ op: "push", state, url }); this.state = state; },
          replaceState(state) { entries.push({ op: "replace", state }); this.state = state; },
        };
        function tick() { return new Promise((resolve) => setImmediate(resolve)); }

        (async () => {
          body.scrollTop = 240;                  // scrolled deep into /projects
          await window.bridgeNavigate("/schedule");
          const afterForward = body.scrollTop;   // forward nav still lands at the top
          const replaced = entries.filter((e) => e.op === "replace");
          const saved = replaced.map((e) => e.state.scroll.body);

          // Back: the browser restores the entry /projects was pushed with.
          const restored = replaced.length ? replaced[0].state : null;
          globalThis.location.href = "http://localhost/projects";
          body.scrollTop = 0;                    // the re-rendered page starts at the top
          globalThis.dispatchEvent({ type: "popstate", state: restored });
          await tick(); await tick(); await tick();

          report({ afterForward, saved, afterBack: body.scrollTop,
                   pushes: entries.filter((e) => e.op === "push").length });
        })();
        """,
        ["shell.js", "router.js"],
        tmp_path,
    )
    assert got["afterForward"] == 0, "a forward navigation still lands at the top"
    assert got["saved"] == [240], (
        "the departing page's scroll offset must be recorded on its own history "
        "entry before the next one is pushed"
    )
    assert got["afterBack"] == 240, (
        "Back re-rendered the page at the top instead of where it was left"
    )
    assert got["pushes"] == 1, "a pop must not push a new history entry"


def test_a_pop_with_no_recorded_position_still_lands_at_the_top(tmp_path):
    """An entry pushed before this shipped (or by a full page load) carries no
    scroll state. That case keeps the old behaviour rather than reading
    `undefined` into `scrollTop`."""
    got = run_js(
        """
        globalThis.window.scrollTo = () => {};
        globalThis.fetch = () => Promise.resolve(
          { ok: true, status: 200, text: async () => "FRAGMENT" });
        const body = document.createElement("div");
        body.setAttribute("class", "shell__body");
        document.body.append(body);
        globalThis.parseFragment = (html) => ({ marker: html });
        globalThis.applyFragment = () => true;
        function tick() { return new Promise((resolve) => setImmediate(resolve)); }

        (async () => {
          body.scrollTop = 180;
          globalThis.location.href = "http://localhost/projects";
          globalThis.dispatchEvent({ type: "popstate", state: null });
          await tick(); await tick(); await tick();
          report({ afterBack: body.scrollTop });
        })();
        """,
        ["shell.js", "router.js"],
        tmp_path,
    )
    assert got["afterBack"] == 0


# --- Pin / hide / restore on /projects: what is said, and where focus lands ---
#
# Every success on /projects re-renders the list through the router, which
# replaces the clicked control and every status node beside it. Hide used to
# write "✓ Hidden" into a status span INSIDE the row it had just removed, so
# nothing was ever announced and focus dropped to <body>. The stub harness in
# test_static_js.py could not see that: its status node is a free global, not
# a child of the row. So this models the real nesting of projects.html and a
# swap that genuinely replaces `.shell__body` with a fresh server render.
#
# minidom models no focus; `El.prototype.focus` is replaced with a recorder, so
# what these prove is WHICH node is handed focus -- and that it is a node of the
# new render, not a detached one -- not how a browser paints the ring.
PROJECTS_PAGE = """
const { El } = require(MINIDOM);
globalThis.window.scrollTo = () => {};
El.prototype.focus = function () { globalThis.__focused = this; };
function tick() { return new Promise((resolve) => setImmediate(resolve)); }
async function settle() { for (let i = 0; i < 20; i += 1) await tick(); }

function el(tag, attrs = {}, kids = []) {
  const node = new El(tag, attrs);
  if (attrs.class) node.setAttribute("class", attrs.class);
  for (const kid of kids) node.append(kid);
  return node;
}

// What the server holds. PATCH mutates it; the fragment render reads it, the way
// the real `/projects` render reads the store after the write lands.
const server = {
  groups: [
    { key: "pinned", open: true, ids: [] },
    { key: "queued", open: true, ids: ["a", "b", "c"] },
    { key: "idle", open: false, ids: ["d"] },
  ],
  hidden: ["h1", "h2"],
};
function take(id) {
  for (const g of server.groups) g.ids = g.ids.filter((x) => x !== id);
  server.hidden = server.hidden.filter((x) => x !== id);
}
function group(key) { return server.groups.find((g) => g.key === key); }

// projects.html's nesting: group <details> > ul > li[data-project-card] >
// Actions <details> > summary + Pin + Hide + the row's own status span.
function renderBody() {
  const filters = ["all", "queued", "hidden"].map((f) => el("button", {
    "data-projects-filter": f, "aria-pressed": f === "all" ? "true" : "false" }));
  const index = el("div", { class: "projects-index", "data-projects-list": "" });
  for (const g of server.groups) {
    if (!g.ids.length) continue;
    const list = el("ul", { class: "projects-list" });
    for (const id of g.ids) {
      list.append(el("li", {
        class: "projects-list__item", "data-project-card": id, "data-project-row-item": "",
        "data-project-state": g.key === "pinned" ? "queued" : g.key,
        "data-project-name": `demo-${id}`, "data-project-path": `/p/${id}`,
      }, [el("details", { class: "projects-list__actions" }, [
        el("summary", {}),
        el("button", { class: "btn btn--pin", "data-project-pin": id,
                       "aria-pressed": g.key === "pinned" ? "true" : "false" }),
        el("button", { class: "btn", "data-project-hide": id }),
        el("span", { "data-project-status": id }),
      ])]));
    }
    const details = el("details", { class: "projects-group", "data-project-group": g.key },
                       [el("summary", { class: "projects-group__head" }), list]);
    details.open = g.open;
    index.append(details);
  }
  const hiddenList = el("ul", { "data-hidden-list": "" }, server.hidden.map((id) =>
    el("li", { "data-hidden-project": id }, [
      el("span", { class: "hidden-project__name" }, []),
      el("button", { class: "btn", "data-project-restore": id }),
    ])));
  hiddenList.children.forEach((li) => {
    li.children[0].textContent = `demo-${li.getAttribute("data-hidden-project")}`;
  });
  const main = el("main", { id: "main" }, [
    el("section", {}, [el("input", { "data-projects-search": "" }), ...filters,
                       el("p", { "data-projects-count": "" })]),
    index,
    el("p", { "data-projects-empty": "", hidden: "" }),
    el("section", { "data-hidden-projects": "", hidden: "" }, [
      hiddenList, el("span", { "data-hidden-status": "" })]),
  ]);
  return el("div", { class: "shell__body" }, [main]);
}

const announcer = el("div", { class: "visually-hidden", role: "status",
                              "data-shell-announce": "" });
const shell = el("div", { class: "shell" }, [renderBody()]);
document.body.append(announcer);
document.body.append(shell);
globalThis.location.href = "http://localhost/projects";

// The swap: the old `.shell__body` goes, a fresh render of the server's current
// state takes its place -- every node the click handler could have held is gone.
globalThis.parseFragment = () => ({ marker: true });
globalThis.applyFragment = () => {
  document.querySelector(".shell__body").remove();
  shell.append(renderBody());
  return true;
};

const seen = { announcedAtPatch: null };
globalThis.fetch = (url, opts) => {
  if (opts && opts.method === "PATCH") {
    seen.announcedAtPatch = announcer.textContent;
    const id = decodeURIComponent(url.split("/").pop());
    const body = JSON.parse(opts.body);
    if (body.status === "hidden") { take(id); server.hidden.push(id); }
    if (body.status === "active") { take(id); group("queued").ids.push(id); }
    if (body.pinned === true) { take(id); group("pinned").ids.push(id); }
    if (body.pinned === false) { take(id); group("idle").ids.push(id); }
    return Promise.resolve({ ok: true, status: 200 });
  }
  return Promise.resolve({ ok: true, status: 200, text: async () => "FRAGMENT" });
};

function focusedIs() {
  const f = globalThis.__focused;
  if (!f) return null;
  const inLiveBody = f.closest(".shell__body") === document.querySelector(".shell__body");
  const row = f.closest("[data-project-card]") || f.closest("[data-hidden-project]");
  return {
    live: inLiveBody,
    tag: f.tag,
    row: row ? (row.getAttribute("data-project-card") || row.getAttribute("data-hidden-project")) : null,
    group: f.closest("[data-project-group]") ? f.closest("[data-project-group]").getAttribute("data-project-group") : null,
    main: f.getAttribute("id") === "main",
    restore: f.getAttribute("data-project-restore"),
  };
}
function click(selector) { document.querySelector(selector).dispatchEvent({ type: "click" }); }
function pressed() {
  const p = document.querySelector('[data-projects-filter][aria-pressed="true"]');
  return p ? p.getAttribute("data-projects-filter") : null;
}
"""


def _run_projects_page(body: str, tmp_path) -> dict:
    return run_js(
        PROJECTS_PAGE.replace("MINIDOM", json.dumps(str(MINIDOM))) + body,
        ["shell.js", "router.js", "projects.js"],
        tmp_path,
    )


def test_hiding_a_row_announces_outside_the_swap_and_focuses_the_next_row(tmp_path):
    got = _run_projects_page(
        """
        (async () => {
          document.querySelector("[data-projects-search]").value = "demo";
          document.querySelector(".shell__body").scrollTop = 240;
          click('[data-project-hide="b"]');
          await settle();
          report({
            announced: announcer.textContent,
            focused: focusedIs(),
            gone: document.querySelector('[data-project-card="b"]') === null,
            search: document.querySelector("[data-projects-search]").value,
            scroll: document.querySelector(".shell__body").scrollTop,
          });
        })();
        """,
        tmp_path,
    )
    assert got["gone"], "the row was not re-rendered away"
    assert got["announced"] == "demo-b hidden from the dashboard", (
        "the success was written somewhere the swap destroyed"
    )
    assert got["focused"] == {"live": True, "tag": "summary", "row": "c", "group": "queued",
                              "main": False, "restore": None}, (
        "focus did not move on to the next row's Actions in the new render"
    )
    assert got["search"] == "demo", "the re-render threw the user's search away"
    assert got["scroll"] == 240, "an in-place re-render jumped to the top"


def test_hiding_the_last_row_hands_focus_back_to_the_one_before_it(tmp_path):
    got = _run_projects_page(
        """
        (async () => {
          take("d");                       // c is now the last row on the page
          document.querySelector(".shell__body").remove();
          shell.append(renderBody());
          click('[data-project-hide="c"]');
          await settle();
          report({ focused: focusedIs() });
        })();
        """,
        tmp_path,
    )
    assert got["focused"]["row"] == "b"
    assert got["focused"]["live"] is True


def test_pinning_follows_the_project_into_its_new_group(tmp_path):
    got = _run_projects_page(
        """
        (async () => {
          click('[data-project-pin="c"]');
          await settle();
          report({ announced: announcer.textContent, focused: focusedIs() });
        })();
        """,
        tmp_path,
    )
    assert got["announced"] == "demo-c pinned"
    assert got["focused"]["row"] == "c"
    assert got["focused"]["group"] == "pinned"
    assert got["focused"]["live"] is True


def test_unpinning_into_a_collapsed_group_focuses_that_groups_summary(tmp_path):
    """A summary inside a closed <details> cannot take focus, so the group's own
    summary -- which names where the project went -- takes it instead."""
    got = _run_projects_page(
        """
        (async () => {
          take("a"); group("pinned").ids.push("a");
          document.querySelector(".shell__body").remove();
          shell.append(renderBody());
          click('[data-project-pin="a"]');
          await settle();
          report({ announced: announcer.textContent, focused: focusedIs() });
        })();
        """,
        tmp_path,
    )
    assert got["announced"] == "demo-a unpinned"
    assert got["focused"]["group"] == "idle"
    assert got["focused"]["row"] is None, "focused a summary inside a collapsed group"
    assert got["focused"]["tag"] == "summary"


def test_restoring_keeps_the_hidden_view_and_moves_on_to_the_next_restore(tmp_path):
    got = _run_projects_page(
        """
        (async () => {
          click('[data-projects-filter="hidden"]');
          click('[data-project-restore="h1"]');
          await settle();
          report({
            announced: announcer.textContent,
            focused: focusedIs(),
            filter: pressed(),
            hiddenShown: !document.querySelector("[data-hidden-projects]").hidden,
          });
        })();
        """,
        tmp_path,
    )
    assert got["announced"] == "demo-h1 restored to the dashboard"
    assert got["filter"] == "hidden", "the re-render dropped the user out of the Hidden view"
    assert got["hiddenShown"] is True
    assert got["focused"]["restore"] == "h2"
    assert got["focused"]["live"] is True


def test_the_announcer_is_emptied_before_the_same_sentence_is_set_again(tmp_path):
    """A live region reads a change. Pinning the same project on a later visit
    sets the sentence already sitting there, which is no change at all -- unless
    the region was emptied first."""
    got = _run_projects_page(
        """
        (async () => {
          announcer.textContent = "demo-c pinned";
          click('[data-project-pin="c"]');
          await settle();
          report({ atPatch: seen.announcedAtPatch, after: announcer.textContent });
        })();
        """,
        tmp_path,
    )
    assert got["atPatch"] == ""
    assert got["after"] == "demo-c pinned"


def test_a_navigation_that_names_no_focus_target_still_lands_on_main_at_the_top(tmp_path):
    """The `focus` option is only for re-renders in place; an ordinary swap keeps
    landing on #main at the top, like a real navigation."""
    got = _run_projects_page(
        """
        (async () => {
          document.querySelector(".shell__body").scrollTop = 240;
          await window.bridgeNavigate("/projects");
          report({ focused: focusedIs(), scroll: document.querySelector(".shell__body").scrollTop });
        })();
        """,
        tmp_path,
    )
    assert got["focused"]["main"] is True
    assert got["scroll"] == 0
