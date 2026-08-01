"""The one piece of JavaScript behaviour that a Python suite cannot infer.

`copy.js` used to read `source.textContent`. On a `<pre>` that is the prompt; on
the `<textarea>` Phase 3 replaces it with, it is the **server-rendered** text and
not what the user typed, so Copy would hand over a stale prompt while looking
like it worked. The plan names this specifically, and everything else in Phase 3
that could go wrong has a test, so this should too.

The rest of the front end is asserted structurally from the rendered HTML in
`test_api.py`. This module exists only because `bridgeText`'s contract is a
property of the DOM object it is handed, which no amount of HTML inspection
reveals. It runs the real file under `node` with a minimal stub rather than
adding a JS test framework, a bundler, or a dependency.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

COPY_JS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "copy.js"

# `tools/falsify.py` runs pytest with `PATH=/usr/bin:/bin`, and Homebrew's node is
# on neither. A bare `shutil.which("node")` therefore skipped this module under
# falsification, pytest exited 0, and the mutation that reverts the `.value` fix
# came back SURVIVED — a skipped test is indistinguishable from a passing one.
# Searching the known absolute locations as well is what makes the mutation real,
# and is the same reason the rest of this repo shells out to absolute paths.
NODE_CANDIDATES = (
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
    "/usr/bin/node",
)


def _node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    return next((p for p in NODE_CANDIDATES if Path(p).exists()), None)

# `copy.js` registers a delegated listener and assigns onto `window` at load, so
# both have to exist before it is evaluated. Nothing here simulates a real DOM:
# the assertion is only about which property `bridgeText` prefers.
HARNESS = """
globalThis.window = globalThis;
globalThis.document = { addEventListener() {}, getElementById: () => null,
                        querySelector: () => null, createRange: () => ({}) };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));
console.log(JSON.stringify({
  // A form control: `.value` is the live, user-edited text.
  textarea: window.bridgeText({ value: "EDITED", textContent: "RENDERED" }),
  // A <pre>: no `.value`, so `.textContent` is still correct.
  pre: window.bridgeText({ textContent: "RENDERED" }),
  // An empty edit must not silently fall back to the rendered text.
  cleared: window.bridgeText({ value: "", textContent: "RENDERED" }),
}));
"""


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_bridge_text_prefers_the_live_value_over_the_rendered_text(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)

    proc = subprocess.run(
        [_node(), str(harness), str(COPY_JS)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)

    assert got["textarea"] == "EDITED", (
        "Copy handed over the server-rendered prompt instead of the user's edit"
    )
    assert got["pre"] == "RENDERED"
    assert got["cleared"] == "", (
        "an emptied textarea fell back to the rendered text, so Copy would "
        "silently resurrect a prompt the user had deleted"
    )


# --- Phase 4 Task 2: the permission mode reaches the request body ------------

LAUNCH_JS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "launch.js"

# `launch.js` registers a delegated click listener, so the harness captures that
# listener at load and invokes it with a synthetic event. Everything the handler
# reaches for is stubbed to a known value, which is what makes the POST body the
# only interesting output. Executed under node resolved by ABSOLUTE PATH: under
# `tools/falsify.py` (PATH=/usr/bin:/bin) a bare `node` would SKIP, and a skipped
# test reports SURVIVED for a mutation it would actually catch.
LAUNCH_HARNESS = """
globalThis.window = globalThis;
let clickHandler = null;
const controls = {
  '[data-launch-model="launch-1"]': { value: "claude-opus-4-8" },
  '[data-launch-effort="launch-1"]': { value: "xhigh" },
  '[data-launch-perm="launch-1"]': { value: PERM_VALUE },
  '[data-launch="launch-1"]': {
    getAttribute: (name) =>
      name === "data-launch-path" ? "/Users/mitsheth/dev/demo" : null,
  },
};
globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  getElementById: () => null,
  querySelector: (sel) => controls[sel] ?? null,
  createRange: () => ({}),
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };
let sentBody = null;
globalThis.fetch = async (url, init) => {
  sentBody = JSON.parse(init.body);
  return { ok: true, status: 200, json: async () => ({ outcome: "started" }) };
};
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const button = {
  getAttribute: () => "launch-1",
  closest: () => button,
  disabled: false,
  setAttribute() {}, removeAttribute() {},
};
clickHandler({ target: { closest: (sel) =>
  sel === "[data-launch-button]" ? button : null } }).then(() => {
  console.log(JSON.stringify(sentBody));
});
"""


def _run_launch_harness(tmp_path, perm_value: str) -> dict:
    harness = tmp_path / "launch_harness.js"
    harness.write_text(LAUNCH_HARNESS.replace("PERM_VALUE", json.dumps(perm_value)))
    proc = subprocess.run(
        [_node(), str(harness), str(LAUNCH_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_launch_js_sends_the_selected_permission_mode(tmp_path):
    """Without this key the server always defaults to none and the select is
    decorative -- it would look armed and launch unarmed."""
    body = _run_launch_harness(tmp_path, "bypassPermissions")
    assert body["permission_mode"] == "bypassPermissions"
    # And the rest of the band still goes with it.
    assert body["model"] == "claude-opus-4-8"
    assert body["effort"] == "xhigh"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_launch_js_sends_the_empty_default_rather_than_omitting_it(tmp_path):
    """The no-flag option posts "", which the server treats as none. Sending it
    explicitly is what keeps the client the only thing deciding the mode."""
    body = _run_launch_harness(tmp_path, "")
    assert body["permission_mode"] == ""


# --- Phase 4 Task 8: live.js behaviour ---------------------------------------

LIVE_JS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "live.js"

# Captures the named listeners at load, then drives them with synthetic frames.
# Node has no EventSource, so it is stubbed to the two methods live.js uses.
LIVE_HARNESS = """
globalThis.window = globalThis;
globalThis.CSS = { escape: (s) => s };
const bands = {};
function band(path) {
  if (!bands[path]) bands[path] = { textContent: "", parentNode: {} };
  return bands[path];
}
band("/p/one");
globalThis.document = {
  addEventListener() {},
  querySelector: (sel) => {
    const m = /\\[data-live-path="(.*)"\\]/.exec(sel);
    return m && bands[m[1]] ? bands[m[1]] : null;
  },
};
let listeners = {};
let closed = 0;
let constructed = 0;
globalThis.EventSource = class {
  constructor() { constructed += 1; }
  addEventListener(name, fn) { listeners[name] = fn; }
  close() { closed += 1; }
};
globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
const delays = [];
globalThis.setTimeout = (fn, ms) => { delays.push(ms); return 0; };
window.setTimeout = globalThis.setTimeout;

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const result = { errors: [] };
const origError = console.error;
console.error = (...a) => result.errors.push(String(a[0]));

SCRIPT

console.error = origError;
result.bands = Object.fromEntries(
  Object.entries(bands).map(([k, v]) => [k, v.textContent]));
result.closed = closed;
result.constructed = constructed;
result.delays = delays;
console.log(JSON.stringify(result));
"""


def _run_live(tmp_path, script: str) -> dict:
    harness = tmp_path / "live_harness.js"
    harness.write_text(LIVE_HARNESS.replace("SCRIPT", script))
    proc = subprocess.run(
        [_node(), str(harness), str(LIVE_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_live_js_patches_a_band_from_a_snapshot(tmp_path):
    got = _run_live(tmp_path, """
listeners.snapshot({ data: JSON.stringify(
  { live: { "/p/one": { status: "busy", started_at: 1 } } }) });
""")
    assert got["bands"]["/p/one"] == "busy"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_live_js_clears_a_band_on_a_tombstone(tmp_path):
    """Without this the card keeps claiming a session that has ended."""
    got = _run_live(tmp_path, """
listeners.snapshot({ data: JSON.stringify(
  { live: { "/p/one": { status: "busy", started_at: 1 } } }) });
listeners.delta({ data: JSON.stringify({ live: {}, removed: ["/p/one"] }) });
""")
    assert got["bands"]["/p/one"] == "ended"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_live_js_skips_a_malformed_frame_and_keeps_going(tmp_path):
    """A bad frame must not kill live updates for the rest of the session."""
    got = _run_live(tmp_path, """
listeners.snapshot({ data: "{ not json" });
listeners.snapshot({ data: JSON.stringify(
  { live: { "/p/one": { status: "idle", started_at: 1 } } }) });
""")
    assert any("malformed" in e for e in got["errors"])
    assert got["bands"]["/p/one"] == "idle", "the stream stopped after one bad frame"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_live_js_only_resets_its_backoff_once_a_connection_has_proved_healthy(tmp_path):
    """An accept-then-close server must not become a hot reconnect loop.

    With zero frames received the connection has proved nothing, so the
    reconnect after a `refresh` is scheduled with a delay rather than
    immediately.
    """
    unhealthy = _run_live(tmp_path, """
// No frames at all: the connection has proved nothing.
listeners.refresh({ data: "{}" });
""")
    assert unhealthy["closed"] == 1
    # Scheduled with a real delay, not immediately.
    assert unhealthy["delays"] == [1000], unhealthy["delays"]

    healthy = _run_live(tmp_path, """
// Two good frames is proof, so the reconnect is immediate.
const frame = JSON.stringify({ live: {} });
listeners.snapshot({ data: frame });
listeners.delta({ data: frame });
listeners.refresh({ data: "{}" });
""")
    assert healthy["delays"] == [0], healthy["delays"]
