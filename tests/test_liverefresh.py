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
