import json, shutil, subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "bridge" / "static"
MINIDOM = Path(__file__).resolve().parent / "js" / "minidom.js"
NODE_CANDIDATES = ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node")

def _node():
    found = shutil.which("node")
    return found or next((p for p in NODE_CANDIDATES if Path(p).exists()), None)

pytestmark = pytest.mark.skipif(_node() is None, reason="node is not installed")

# Harness prelude: a .shell__body in the document, synchronous-resolving fetch
# that records calls and returns a canned fragment, and stubs for the three
# window.bridge* collaborators. setTimeout in minidom is a no-op returning 0, so
# the controller must expose a synchronous _refreshNow seam the tests drive.
PRELUDE = """
function setPath(p) { globalThis.location.href = "http://localhost" + p; }
function shellBody() {
  const b = document.createElement("div"); b.setAttribute("class", "shell__body");
  document.body.append(b); return b;
}
globalThis.fetch = (url, opts) => {
  globalThis.__calls.fetch.push({ url, opts });
  return Promise.resolve({ ok: true, status: 200, text: async () => "FRAGMENT" });
};
"""

def _run(body: str, tmp_path):
    script = tmp_path / "case.js"
    script.write_text(
        f'const {{ makeDocument, load, report }} = require({json.dumps(str(MINIDOM))});\n'
        f'makeDocument(null);\n'
        f'load({json.dumps(str(STATIC / "shell.js"))});\n'
        f'load({json.dumps(str(STATIC / "morph.js"))});\n'
        # bridgeLive + bridgeFragment stubs BEFORE the controller loads and reads them.
        f'window.bridgeLive = {{ _fns: [], onFrame(fn){{ this._fns.push(fn); }} }};\n'
        f'window.bridgeFragment = {{ parse: (html) => window.__parsed }};\n'
        f'load({json.dumps(str(STATIC / "liverefresh.js"))});\n'
        f'{PRELUDE}\n{body}\n'
    )
    proc = subprocess.run([_node(), str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)

def test_generation_bump_on_an_owned_route_refreshes(tmp_path):
    got = _run("""
        (async () => {
        setPath("/schedule");
        const body = shellBody();
        const inc = document.createElement("div"); inc.setAttribute("class", "shell__body");
        inc.append(document.createElement("p")); window.__parsed = { body: inc };
        window.bridgePage.enter();                          // baseline generation read
        window.bridgeLiveRefresh._onFrame({ generation: 1 });
        window.bridgeLiveRefresh._onFrame({ generation: 2 });   // bump -> refresh
        window.bridgeLiveRefresh._refreshNow();
        // _refreshNow's fetch/parse/morph chain is genuinely async (a real
        // Promise's `.then` always defers to a microtask); a macrotask
        // boundary lets every hop of that chain settle before we inspect the
        // DOM it mutates.
        await new Promise((resolve) => setImmediate(resolve));
        report({ fetches: globalThis.__calls.fetch.length,
                 url: globalThis.__calls.fetch[0] && globalThis.__calls.fetch[0].url,
                 fragHeader: globalThis.__calls.fetch[0].opts.headers["X-Bridge-Fragment"],
                 morphed: body.children.length });
        })();
    """, tmp_path)
    assert got["fetches"] == 1
    assert got["url"] == "http://localhost/schedule"
    assert got["fragHeader"] == "1"
    assert got["morphed"] == 1, "the fetched fragment body was morphed in"

def test_unowned_route_never_refreshes(tmp_path):
    got = _run("""
        setPath("/");                                   // Overview -- not owned
        shellBody();
        window.bridgePage.enter();
        window.bridgeLiveRefresh._onFrame({ generation: 9 });
        window.bridgeLiveRefresh._refreshNow();
        report({ fetches: globalThis.__calls.fetch.length });
    """, tmp_path)
    assert got["fetches"] == 0

def test_no_refresh_without_a_generation_bump(tmp_path):
    got = _run("""
        setPath("/diagnostics");
        shellBody();
        window.bridgePage.enter();                       // baseline = last seen
        window.bridgeLiveRefresh._onFrame({ generation: 5 });
        window.bridgeLiveRefresh._onFrame({ generation: 5 });  // same -> nothing
        window.bridgeLiveRefresh._refreshNow();
        report({ fetches: globalThis.__calls.fetch.length });
    """, tmp_path)
    assert got["fetches"] == 0

def test_focus_in_a_protected_node_defers_the_refresh(tmp_path):
    got = _run("""
        setPath("/project/12");
        const body = shellBody();
        const ta = document.createElement("textarea");
        ta.setAttribute("data-live-preserve", ""); body.append(ta);
        document.activeElement = ta;                     // simulate focus
        const inc = document.createElement("div"); inc.setAttribute("class", "shell__body");
        window.__parsed = { body: inc };
        window.bridgePage.enter();
        window.bridgeLiveRefresh._onFrame({ generation: 3 });
        window.bridgeLiveRefresh._refreshNow();          // unsafe -> deferred
        const deferred = globalThis.__calls.fetch.length;
        document.activeElement = null;                   // focus leaves
        window.bridgeLiveRefresh._onFrame({ generation: 4 });
        window.bridgeLiveRefresh._refreshNow();          // now safe
        report({ deferred, after: globalThis.__calls.fetch.length });
    """, tmp_path)
    assert got["deferred"] == 0, "must not refresh while a protected node has focus"
    assert got["after"] == 1

def test_navigating_away_during_an_in_flight_fetch_does_not_clobber_the_new_route(tmp_path):
    got = _run("""
        (async () => {
        setPath("/schedule");
        const body = shellBody();
        const marker = document.createElement("span");
        marker.setAttribute("data-marker", "original"); body.append(marker);
        const inc = document.createElement("div"); inc.setAttribute("class", "shell__body");
        inc.append(document.createElement("p")); window.__parsed = { body: inc };

        window.bridgePage.enter();                            // land on /schedule
        window.bridgeLiveRefresh._onFrame({ generation: 1 });  // baseline = 1
        window.bridgeLiveRefresh._onFrame({ generation: 2 });  // bump -> pending
        window.bridgeLiveRefresh._refreshNow();                // fetch #1: in flight, unresolved

        // Navigate to ANOTHER owned route while fetch #1 is still pending --
        // `owned` stays true, only the path differs, so this exercises the
        // path check specifically, not just the owned flag.
        await window.bridgePage.leave();
        setPath("/diagnostics");
        window.bridgePage.enter();

        // Let fetch #1's chain fully settle now that the route has moved on.
        await new Promise((resolve) => setImmediate(resolve));

        const clobbered = body.children.length !== 1 || marker.parent !== body;

        // Back on /schedule, a fresh bump must still get its own fetch --
        // proof the earlier in-flight fetch never silently consumed this one
        // by corrupting the baseline.
        setPath("/schedule");
        window.bridgePage.enter();
        window.bridgeLiveRefresh._onFrame({ generation: 3 });
        window.bridgeLiveRefresh._refreshNow();                // fetch #2

        report({ clobbered, fetches: globalThis.__calls.fetch.length });
        })();
    """, tmp_path)
    assert got["clobbered"] is False, "a stale fetch must never morph into the new route's DOM"
    assert got["fetches"] == 2, "the same-route bump after returning must still get its own fetch"

def test_focus_on_a_non_preserve_node_does_not_abort_the_whole_refresh(tmp_path):
    # morph.js calls opts.ignore on the ROOT .shell__body first. The old
    # ignoreNode returned true whenever the root `.contains(activeElement)` --
    # true any time focus is anywhere in the content area -- so the entire
    # morph was skipped. A click-focused button (not a data-live-preserve
    # node) must NOT have that effect: only the exact focused node (and any
    # data-live-preserve node) may be left untouched; the rest of the tree
    # still morphs.
    got = _run("""
        (async () => {
        setPath("/schedule");
        const body = shellBody();
        const btn = document.createElement("button"); body.append(btn);
        document.activeElement = btn;                     // simulate click-focus
        const inc = document.createElement("div"); inc.setAttribute("class", "shell__body");
        inc.append(document.createElement("p")); window.__parsed = { body: inc };
        window.bridgePage.enter();
        window.bridgeLiveRefresh._onFrame({ generation: 1 });
        window.bridgeLiveRefresh._onFrame({ generation: 2 });   // bump -> refresh
        window.bridgeLiveRefresh._refreshNow();
        await new Promise((resolve) => setImmediate(resolve));
        const tags = Array.from(body.children).map((el) => el.localName || el.tag);
        report({ tags, stillFocused: document.activeElement === btn });
        })();
    """, tmp_path)
    # Pre-fix, ignoreNode(root) short-circuits the whole morph and `body`
    # keeps its single original <button> child forever -- this assertion
    # fails against that code.
    assert "p" in got["tags"], "the incoming fragment must be morphed in, not aborted at the root"
    assert "button" in got["tags"], "the focused node itself is still preserved in place"
    assert got["stillFocused"] is True

def test_a_newer_bump_during_an_in_flight_fetch_is_not_dropped(tmp_path):
    # If a fresher generation arrives from onFrame while a refresh fetch is
    # still in flight, the success handler must not blindly null out
    # pendingGeneration -- doing so would silently drop that newer bump and
    # the view would never catch up to it. Observe this by checking that a
    # later _refreshNow() still issues a fetch for the retained bump.
    got = _run("""
        (async () => {
        setPath("/schedule");
        shellBody();
        const inc = document.createElement("div"); inc.setAttribute("class", "shell__body");
        window.__parsed = { body: inc };

        window.bridgePage.enter();
        window.bridgeLiveRefresh._onFrame({ generation: 1 });   // baseline = 1
        window.bridgeLiveRefresh._onFrame({ generation: 2 });   // bump -> pending = 2
        window.bridgeLiveRefresh._refreshNow();                 // fetch #1 in flight

        // A fresher frame lands before fetch #1's .then settles.
        window.bridgeLiveRefresh._onFrame({ generation: 3 });   // pending -> 3

        await new Promise((resolve) => setImmediate(resolve));  // let fetch #1 settle
        const fetchesAfterFirst = globalThis.__calls.fetch.length;

        window.bridgeLiveRefresh._refreshNow();                 // should still fire: gen 3 pending
        report({ fetchesAfterFirst, fetchesAfterSecondCall: globalThis.__calls.fetch.length });
        })();
    """, tmp_path)
    assert got["fetchesAfterFirst"] == 1
    assert got["fetchesAfterSecondCall"] == 2, (
        "the newer pending generation (3) must survive fetch #1's success handler "
        "so a subsequent refresh still fetches instead of no-op'ing"
    )

def test_a_debounced_burst_schedules_only_one_refresh(tmp_path):
    got = _run("""
        setPath("/schedule");
        shellBody();
        window.bridgePage.enter();
        window.bridgeLiveRefresh._onFrame({ generation: 1 });   // baseline = 1

        // minidom's default setTimeout is a no-op that returns 0 (falsy), which
        // would defeat the `if (timer) return` coalescing check regardless of
        // whether it works -- a real browser's setTimeout returns a nonzero id.
        // Swap in a stub that mimics that truthiness so the coalescing branch
        // is actually exercised.
        let scheduled = 0;
        globalThis.setTimeout = (fn, ms) => { scheduled += 1; return scheduled; };

        window.bridgeLiveRefresh._onFrame({ generation: 2 });   // schedules once
        window.bridgeLiveRefresh._onFrame({ generation: 3 });   // same burst -> coalesced
        report({ scheduled });
    """, tmp_path)
    assert got["scheduled"] == 1, "a burst of bumps within the debounce window must schedule once"
