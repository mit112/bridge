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
import os
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
// `launch.js` registers more than one delegated "click" listener (the launch
// button, and Task 3.3's handoff-dismiss button) -- a single `clickHandler`
// slot would let the second registration silently replace the first. An
// array plus `Promise.all` mirrors how the real DOM dispatches one event to
// every listener: each async handler that does not match its own selector
// returns immediately, so only the matching one does any real work.
let clickHandlers = [];
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
  addEventListener(type, fn) { if (type === "click") clickHandlers.push(fn); },
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
const event = { target: { closest: (sel) =>
  sel === "[data-launch-button]" ? button : null } };
Promise.all(clickHandlers.map((fn) => fn(event))).then(() => {
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


# --- Empty-state primary action drives the compose textarea, never a 422 -----
#
# With no queued handoff, the workspace's "Start session" band names the ad hoc
# compose textarea in `data-launch-prompt` and renders the button `disabled`.
# This harness proves the button enables only once the compose field has text,
# that an empty compose fires no /api/launch (the request that used to 422), and
# that a launch with text carries that text and no handoff id.
EMPTY_STATE_HARNESS = """
globalThis.window = globalThis;
let clickHandlers = [];
let inputHandlers = [];

const composeField = {
  id: "compose-1", value: "",
  closest: (sel) => (sel === "[data-compose-prompt]" ? composeField : null),
};
const button = {
  disabled: true, attrs: { "data-launch-button": "launch-1" },
  getAttribute(n) { return this.attrs[n] ?? null; },
  setAttribute() {}, removeAttribute() {},
  closest: (sel) => (sel === "[data-launch-button]" ? button : null),
};
const band = {
  attrs: { "data-launch": "launch-1", "data-launch-path": "/Users/mitsheth/dev/demo",
           "data-launch-prompt": "compose-1" },
  getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; },
  querySelector: (sel) => (sel === "[data-launch-button]" ? button : null),
};
const statusNode = { textContent: "" };
const controls = {
  '[data-launch="launch-1"]': band,
  '[data-launch-prompt="compose-1"]': band,
  '[data-launch-status="launch-1"]': statusNode,
  '[data-launch-model="launch-1"]': { value: "claude-opus-4-8" },
  '[data-launch-effort="launch-1"]': { value: "high" },
  '[data-launch-perm="launch-1"]': { value: "" },
};
globalThis.document = {
  addEventListener(type, fn) {
    if (type === "click") clickHandlers.push(fn);
    if (type === "input") inputHandlers.push(fn);
  },
  getElementById: (id) => (id === "compose-1" ? composeField : null),
  querySelector: (sel) => controls[sel] ?? null,
  createRange: () => ({}),
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };
let fetchCount = 0;
let sentBody = null;
globalThis.fetch = async (url, init) => {
  fetchCount += 1;
  sentBody = JSON.parse(init.body);
  return { ok: true, status: 200, json: async () => ({ outcome: "started" }) };
};
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const fireInput = () => Promise.all(inputHandlers.map((fn) =>
  fn({ target: { closest: (sel) => composeField.closest(sel) } })));
const fireClick = () => Promise.all(clickHandlers.map((fn) =>
  fn({ target: { closest: (sel) => button.closest(sel) } })));

(async () => {
  await fireInput();
  const disabledWhenEmpty = button.disabled;
  const fetchAfterEmpty = fetchCount;
  composeField.value = "run this now";
  await fireInput();
  const enabledWhenTyped = button.disabled;
  await fireClick();
  console.log(JSON.stringify({
    disabledWhenEmpty, fetchAfterEmpty, enabledWhenTyped,
    fetchCount, prompt: sentBody ? sentBody.prompt : null,
    handoffId: sentBody ? sentBody.handoff_id : null,
  }));
})();
"""


def _run_empty_state(tmp_path) -> dict:
    harness = tmp_path / "empty_state_harness.js"
    harness.write_text(EMPTY_STATE_HARNESS)
    proc = subprocess.run(
        [_node(), str(harness), str(LAUNCH_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_empty_state_primary_button_stays_disabled_and_never_fires_a_promptless_launch(
    tmp_path,
):
    got = _run_empty_state(tmp_path)
    assert got["disabledWhenEmpty"] is True
    assert got["fetchAfterEmpty"] == 0, "an empty compose still fired a launch (422)"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_empty_state_primary_button_enables_and_launches_the_composed_prompt(tmp_path):
    got = _run_empty_state(tmp_path)
    assert got["enabledWhenTyped"] is False, "typing a prompt did not enable the button"
    assert got["fetchCount"] == 1
    assert got["prompt"] == "run this now"
    # No queued handoff to attach: an ad hoc launch carries no handoff id.
    assert got["handoffId"] is None


# --- Task 3.3 fix round: the "Dismiss handoff" click handler -----------------
#
# `launch.js` registers a SECOND delegated "click" listener for
# `[data-handoff-dismiss]` (Task 3.3). This harness mirrors `LAUNCH_HARNESS`'s
# shape -- a stub DOM keyed by selector, a captured `fetch` call, the real
# file evaluated under `node` -- but drives that second listener instead of
# the launch-button one, and inspects the DOM nodes the handler is supposed to
# patch directly (never `innerHTML`, never a reload).
DISMISS_HARNESS = """
globalThis.window = globalThis;
let clickHandlers = [];
const section = { hidden: false };
const launchButton = { textContent: "Continue in Terminal" };
const band = {
  attrs: { "data-launch-handoff": "h1", "data-launch-prompt": "handoff-h1" },
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; },
  removeAttribute(name) { delete this.attrs[name]; },
  querySelector(sel) { return sel === "[data-launch-button]" ? launchButton : null; },
};
const empty = { hidden: true };
const status = { textContent: "" };
const controls = {
  '[data-handoff-section="h1"]': section,
  '[data-launch-handoff="h1"]': band,
  "[data-handoff-empty]": empty,
  '[data-handoff-dismiss-status="h1"]': status,
};
globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandlers.push(fn); },
  querySelector: (sel) => controls[sel] ?? null,
};
// A reload or a `location.href` assignment would be the "no reload" contract
// broken -- neither is ever referenced by the handler, but stubbing both as
// spies is what lets the test say so affirmatively rather than by omission.
let reloadCalled = false;
let hrefAssigned = false;
globalThis.location = {
  reload() { reloadCalled = true; },
  get href() { return "http://127.0.0.1:8787/project/1?tab=current"; },
  set href(_v) { hrefAssigned = true; },
};
const fetchCalls = [];
globalThis.fetch = async (url, init) => {
  fetchCalls.push({ url, method: init.method, body: init.body });
  return { ok: true, status: 200, json: async () => ({}) };
};
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const button = {
  getAttribute: () => "h1",
  disabled: false,
  closest: (sel) => (sel === "[data-handoff-dismiss]" ? button : null),
};
const event = { target: button };
Promise.all(clickHandlers.map((fn) => fn(event))).then(() => {
  console.log(JSON.stringify({
    fetchCalls,
    sectionHidden: section.hidden,
    emptyHidden: empty.hidden,
    bandStillHasHandoff: Object.prototype.hasOwnProperty.call(band.attrs, "data-launch-handoff"),
    launchButtonText: launchButton.textContent,
    statusText: status.textContent,
    reloadCalled,
    hrefAssigned,
  }));
});
"""


def _run_dismiss_harness(tmp_path) -> dict:
    harness = tmp_path / "dismiss_harness.js"
    harness.write_text(DISMISS_HARNESS)
    proc = subprocess.run(
        [_node(), str(harness), str(LAUNCH_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dismiss_handoff_patches_the_existing_endpoint_with_no_new_write_path(
    tmp_path,
):
    result = _run_dismiss_harness(tmp_path)
    assert len(result["fetchCalls"]) == 1
    call = result["fetchCalls"][0]
    assert call["url"] == "/api/handoff/h1"
    assert call["method"] == "PATCH"
    assert json.loads(call["body"]) == {"status": "dismissed"}


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dismiss_handoff_updates_the_dom_in_place_on_success(tmp_path):
    result = _run_dismiss_harness(tmp_path)
    # The handoff section hides and the empty state reveals -- both by
    # flipping `hidden` on already-rendered nodes, never by rebuilding either
    # one's markup.
    assert result["sectionHidden"] is True
    assert result["emptyHidden"] is False
    # The launch band that was driving the now-dismissed handoff is demoted:
    # the attribute naming it is gone, and its primary button falls back to
    # the empty-state label.
    assert result["bandStillHasHandoff"] is False
    assert result["launchButtonText"] == "Start session"
    # A role=status announcement, not a reload.
    assert "Dismissed" in result["statusText"]
    assert result["reloadCalled"] is False
    assert result["hrefAssigned"] is False


# --- Phase 4 Task 8: live.js behaviour ---------------------------------------

LIVE_JS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "live.js"

# Captures the named listeners at load, then drives them with synthetic frames.
# Node has no EventSource, so it is stubbed to the two methods live.js uses.
LIVE_HARNESS = """
globalThis.window = globalThis;
globalThis.CSS = { escape: (s) => s };
const bands = {};
function band(path) {
  if (!bands[path]) {
    const classes = new Set(["live", "live--unknown"]);
    bands[path] = {
      textContent: "",
      parentNode: {},
      classList: {
        add: (...names) => names.forEach((name) => classes.add(name)),
        remove: (...names) => names.forEach((name) => classes.delete(name)),
        values: () => [...classes],
      },
    };
  }
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
result.bandClasses = Object.fromEntries(
  Object.entries(bands).map(([k, v]) => [k, v.classList.values().sort()]));
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
    assert got["bandClasses"]["/p/one"] == ["live", "live--busy"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_live_js_clears_a_band_on_a_tombstone(tmp_path):
    """Without this the card keeps claiming a session that has ended."""
    got = _run_live(tmp_path, """
listeners.snapshot({ data: JSON.stringify(
  { live: { "/p/one": { status: "busy", started_at: 1 } } }) });
listeners.delta({ data: JSON.stringify({ live: {}, removed: ["/p/one"] }) });
""")
    assert got["bands"]["/p/one"] == "ended"
    assert got["bandClasses"]["/p/one"] == ["live", "live--ended"]


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


FRESHNESS_HARNESS = r'''
globalThis.window = globalThis;
globalThis.CSS = { escape: (s) => s };
let now = 100;
Date.now = () => now * 1000;
let clickHandler = null;
const listeners = {};
const announcements = [];

function classes() {
  const values = new Set();
  return {
    add: (...names) => names.forEach((name) => values.add(name)),
    remove: (...names) => names.forEach((name) => values.delete(name)),
    values: () => [...values],
  };
}
function node(attrs = {}) {
  return {
    attrs: { ...attrs }, hidden: false, textContent: "", children: [],
    classList: classes(),
    getAttribute(name) { return this.attrs[name] ?? null; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    removeAttribute(name) { delete this.attrs[name]; },
    append(child) {
      this.children = this.children.filter((item) => item !== child);
      this.children.push(child); child.parentNode = this;
    },
    querySelector() { return null; },
    closest() { return null; },
  };
}

const strip = node({ "data-index-at": "100", "data-generation": "0", "data-server": "available" });
const label = node();
Object.defineProperty(label, "textContent", {
  get() { return this._text || ""; },
  set(value) { this._text = String(value); announcements.push(this._text); },
});
const age = node();
const membership = node();
const diagnostics = node();
const totals = {};
for (const name of ["projects", "running", "queued", "scheduled", "today", "last_5h", "burn_rate", "last_index"]) totals[name] = node();
const refreshButton = node();
const refreshStatus = node();

function card(id) {
  const root = node({ "data-project-card": String(id) });
  const leaves = {
    "[data-live-status]": node(), "[data-live-age]": node(),
    "[data-live-model]": node(), "[data-live-effort]": node(),
    "[data-git-branch]": node(), "[data-git-dirty]": node(),
    "[data-git-ahead]": node(), "[data-git-stale]": node(),
    "[data-git-cache]": node(), '[data-git-status="not_a_repo"]': node(),
    '[data-git-status="unavailable"]': node(), "[data-burn-today]": node(),
    "[data-burn-last-5h]": node(), "[data-sparkline]": node(),
  };
  const bandParent = node();
  leaves["[data-live-status]"].closest = () => bandParent;
  root.querySelector = (sel) => leaves[sel] || null;
  root.textarea = { value: `typed ${id}` };
  return root;
}
const cards = [card("1"), card("2")];
const list = node();
[cards[1], cards[0]].forEach((item) => list.append(item));
const cardMap = Object.fromEntries(cards.map((item) => [item.getAttribute("data-project-card"), item]));

const selectors = {
  "[data-freshness-strip]": strip, "[data-freshness-label]": label,
  "[data-freshness-age]": age, "[data-project-membership-status]": membership,
  "[data-diagnostics-alert]": diagnostics, "[data-cards-list]": list,
  "[data-dashboard-refresh]": refreshButton, "[data-refresh-status]": refreshStatus,
};
for (const [name, value] of Object.entries(totals)) selectors[`[data-dashboard-total="${name}"]`] = value;
selectors['[data-dashboard-total="scheduled"]'] = node();
selectors['[data-dashboard-total="scheduled"].dd'] = totals.scheduled;
selectors['[data-topbar-scheduled]'] = totals.scheduled;
selectors['[data-dashboard-total="scheduled"]'].querySelector = () => totals.scheduled;

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  querySelector(sel) {
    if (selectors[sel]) return selectors[sel];
    const cardMatch = /^\[data-project-card="(.*)"\]$/.exec(sel);
    return cardMatch ? cardMap[cardMatch[1]] : null;
  },
  querySelectorAll(sel) { return sel === "[data-project-card]" ? cards : []; },
};
globalThis.EventSource = class {
  constructor() { this.listeners = listeners; }
  addEventListener(name, fn) { listeners[name] = fn; }
  close() {}
};
globalThis.setTimeout = (fn) => { fn(); return 0; };
window.setTimeout = globalThis.setTimeout;
globalThis.fetch = async () => ({ ok: true, json: async () => REFRESH_BODY });
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const textareaIdentity = cards.map((item) => item.textarea);
function frame(kind, generation, indexAt, order) {
  return { schema: 1, kind, generated_at: 999999, generation,
    freshness: { server: "available", index_at: indexAt, index_age_seconds: 0 },
    topbar: { projects: 2, running: 1, queued: 1, scheduled: 0, today: 1200,
      last_5h: 2400, burn_rate: 480, last_index: indexAt },
    diagnostics: { alert: true }, card_order: order,
    cards: { "1": { live: { available: true, status: "idle", started_at: 1 },
      git: { status: "ok", branch: "main", dirty_count: 2, ahead: 1, stale: false },
      burn: { today: 1200, last_5h: 2400, spark_points: "0,20 72,0" } } },
    refresh: { attempted: false, completed: true, error: null }, unattributed: [] };
}

const full = frame("snapshot", 0, 100, ["2", "1"]);
REFRESH_BODY = frame("snapshot", 2, 146, ["1", "2"]);
window.bridgeApplyDashboardUpdate(full);
now = 146;
window.bridgeApplyDashboardUpdate({ schema: 1, kind: "patch", generated_at: 146,
  generation: 0, freshness: { server: "available", index_at: 146, index_age_seconds: 0 },
  cards: { "1": { live: { available: true, status: "busy", started_at: 2 } } } });
const stale = label.textContent;
window.bridgeApplyDashboardUpdate(REFRESH_BODY);
const changedIds = list.children.map((item) => item.getAttribute("data-project-card"));
membership.textContent = "";
window.bridgeApplyDashboardUpdate({ schema: 1, kind: "patch", generated_at: 147,
  generation: 2, freshness: { server: "available", index_at: 146, index_age_seconds: 1 },
  card_order: ["1", "3"], cards: {} });
const membershipText = membership.textContent;
window.bridgeApplyDashboardUpdate({ schema: 1, kind: "snapshot", generated_at: 148,
  generation: 3, freshness: { server: "unavailable", index_at: null, index_age_seconds: null },
  topbar: {}, diagnostics: { alert: true }, card_order: [], cards: {},
  refresh: { attempted: true, completed: false, error: "offline" }, unattributed: [] });
const unavailableState = label.textContent;
window.bridgeApplyDashboardUpdate(REFRESH_BODY);
const beforeRefresh = refreshStatus.textContent;
clickHandler({ target: { closest: (sel) => sel === "[data-dashboard-refresh]" ? refreshButton : null } });
setImmediate(() => console.log(JSON.stringify({
  stale, fresh: label.textContent, unavailableState, changedIds, membershipText,
  announcements,
  textareaSame: textareaIdentity.every((item, index) => item === cards[index].textarea),
  textareaValues: textareaIdentity.map((item) => item.value),
  refresh: refreshStatus.textContent, totals: totals.today.textContent,
  lastIndex: totals.last_index.textContent,
  beforeRefresh,
})));
'''


def _run_freshness(tmp_path, refresh_body):
    harness = tmp_path / "freshness_harness.js"
    harness.write_text("let REFRESH_BODY = " + json.dumps(refresh_body) + ";\n" + FRESHNESS_HARNESS)
    proc = subprocess.run(
        [_node(), str(harness), str(LIVE_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dashboard_updates_keep_index_freshness_separate_from_liveness(tmp_path):
    body = {"schema": 1, "kind": "snapshot", "generated_at": 1, "generation": 2,
            "freshness": {"server": "available", "index_at": 146, "index_age_seconds": 0},
            "topbar": {}, "diagnostics": {"alert": False}, "card_order": [], "cards": {},
            "refresh": {"attempted": False, "completed": True, "error": None}, "unattributed": []}
    got = _run_freshness(tmp_path, body)
    assert got["stale"] == "stale"
    assert got["fresh"] == "connected"
    assert got["unavailableState"] == "unavailable"
    assert got["totals"] == "1k"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_topbar_last_index_is_left_server_rendered_not_overwritten_with_a_raw_epoch(
    tmp_path,
):
    """Every frame the harness drives carries `topbar.last_index` as a raw epoch
    (146). If the generic topbar loop patched it, the server-rendered "Xm ago"
    would be replaced by "146" on the first tick. Excluding it keeps the node
    untouched (still the empty server value in this stub), never a bare epoch."""
    got = _run_freshness(tmp_path, {})
    assert got["lastIndex"] == "", (
        "last_index was patched with a raw epoch instead of being left as the "
        "server-rendered relative time"
    )


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dashboard_refresh_reorders_existing_nodes_and_preserves_user_text(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["changedIds"] == ["1", "2"]
    assert got["textareaSame"] is True
    assert got["textareaValues"] == ["typed 1", "typed 2"]
    assert got["membershipText"] == "Project list changed - reopen the panel to update cards."
    assert got["refresh"] == "Updated"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_liveness_patch_does_not_reset_index_freshness(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["stale"] == "stale"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_generated_at_is_not_index_freshness_clock(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["stale"] == "stale"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_stale_threshold_is_45_seconds(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["stale"] == "stale"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_connection_states_do_not_announce_heartbeats(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["announcements"] == [
        "connected", "stale", "connected", "unavailable", "connected",
    ]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_membership_drift_is_non_alarm_and_identity_safe(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["membershipText"].startswith("Project list changed")
    assert got["unavailableState"] == "unavailable"
    assert got["textareaSame"] is True


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_unavailable_snapshot_is_distinct_from_stale_project(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["unavailableState"] == "unavailable"
    assert got["stale"] == "stale"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_snapshot_reorders_existing_cards_by_server_order(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["changedIds"] == ["1", "2"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dashboard_patch_preserves_card_and_textarea_identity(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["textareaSame"] is True


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dashboard_patch_preserves_user_edited_textarea_value(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["textareaValues"] == ["typed 1", "typed 2"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_refresh_button_posts_and_applies_snapshot(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["refresh"] == "Updated"


# --- Task 2.4: live.js tolerates the leaf-light Overview DOM ----------------
#
# Overview (`/`) renders totals, a freshness strip, and (at most) a live-status
# word -- it has no `[data-cards-list]`, and the one card-shaped element it
# might address has none of the `[data-git-*]`/`[data-burn-*]`/
# `[data-sparkline]` leaves `patchGit`/`patchBurn` look for (those hooks now
# have zero renderers anywhere in the app; git is static text in the
# workspace). Its freshness strip also never carries `data-generation`/
# `data-generated-at`: `OverviewModel.freshness` has no server generation
# counter, so `overview.html`'s call to `freshness_status()` omits both
# kwargs, and the macro's own `{% if generated_at is not none %}` guard skips
# rendering the attribute entirely. This harness builds exactly that sparser
# DOM (not the dashboard-shaped stub the harnesses above use) and drives
# `bridgeApplyDashboardUpdate` -- the schema-1 entry point every SSE frame and
# the Refresh button both call -- straight at it.
OVERVIEW_HARNESS = r'''
globalThis.window = globalThis;
globalThis.CSS = { escape: (s) => s };
let now = 100;
Date.now = () => now * 1000;

function classes() {
  const values = new Set();
  return {
    add: (...names) => names.forEach((name) => values.add(name)),
    remove: (...names) => names.forEach((name) => values.delete(name)),
    values: () => [...values],
  };
}
function node(attrs = {}) {
  return {
    attrs: { ...attrs }, hidden: false, textContent: "",
    classList: classes(),
    getAttribute(name) { return this.attrs[name] ?? null; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    removeAttribute(name) { delete this.attrs[name]; },
    closest() { return null; },
  };
}

// The real freshness_status() macro only emits data-generated-at/
// data-generation when the caller passes them -- overview.html's call does
// not. Only data-index-at and data-server are unconditionally rendered.
const strip = node({ "data-index-at": "100", "data-server": "available" });
const label = node();
const age = node();
const diagnostics = node();
const totals = {};
for (const name of ["projects", "running", "queued", "scheduled", "today",
                     "last_5h", "burn_rate", "last_index"]) totals[name] = node();

// One leaf-light card: a live-status word and nothing else. querySelector is
// a real function (as every actual DOM element's is) that simply has no
// match for a git/burn/sparkline selector -- the realistic "leaf absent"
// case, not a missing method.
const liveWord = node();
liveWord.attrs["data-live-path"] = "/Users/mitsheth/dev/demo";
const projectCard = node({ "data-project-card": "1" });
projectCard.querySelector = (sel) => (sel === "[data-live-status]" ? liveWord : null);

const selectors = {
  "[data-freshness-strip]": strip, "[data-freshness-label]": label,
  "[data-freshness-age]": age, "[data-diagnostics-alert]": diagnostics,
  '[data-project-card="1"]': projectCard,
};
for (const [name, value] of Object.entries(totals)) selectors[`[data-dashboard-total="${name}"]`] = value;

globalThis.document = {
  addEventListener() {},
  querySelector(sel) { return selectors[sel] ?? null; },
  querySelectorAll() { return []; },
};
globalThis.EventSource = class { addEventListener() {} close() {} };
globalThis.setTimeout = (fn) => { fn(); return 0; };
window.setTimeout = globalThis.setTimeout;
globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const errors = [];
console.error = (...a) => errors.push(String(a[0]));

// Bumped after boot so a stale-vs-fresh outcome actually depends on whether
// the frame below was accepted, not on whatever the boot-time index_at
// already matched.
now = 246;

const frame = {
  schema: 1, kind: FRAME_KIND, generated_at: 246, generation: FRAME_GENERATION,
  freshness: { server: "available", index_at: 246, index_age_seconds: 0 },
  topbar: { projects: 1, running: 1, queued: 0, scheduled: 0, today: 1200,
    last_5h: 2400, burn_rate: 480, last_index: 246 },
  diagnostics: { alert: false },
  cards: {
    // Leaf-light card: real git/burn payloads arrive (the server still
    // computes them for the shared envelope) even though nothing in the DOM
    // renders them.
    "1": { live: { available: true, status: "busy", started_at: 1 },
           git: { status: "ok", branch: "main", dirty_count: 2 },
           burn: { today: 1200, last_5h: 2400, spark_points: "0,20 72,0" } },
    // No `[data-project-card="2"]` exists at all -- iterating cards must
    // tolerate one that is simply not on the page.
    "2": { live: { available: true, status: "idle", started_at: 1 } },
  },
};

let threw = null;
try {
  window.bridgeApplyDashboardUpdate(frame);
} catch (error) {
  threw = String(error);
}

console.log(JSON.stringify({
  threw, errors,
  totalsToday: totals.today.textContent,
  freshnessLabel: label.textContent,
  liveWordText: liveWord.textContent,
}));
'''


def _run_overview_dom(tmp_path, frame_kind: str, frame_generation) -> dict:
    harness = tmp_path / "overview_harness.js"
    harness.write_text(
        OVERVIEW_HARNESS.replace("FRAME_KIND", json.dumps(frame_kind))
        .replace("FRAME_GENERATION", json.dumps(frame_generation))
    )
    proc = subprocess.run(
        [_node(), str(harness), str(LIVE_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_apply_dashboard_update_tolerates_the_leaf_light_overview_dom(tmp_path):
    """No `[data-cards-list]`, no git/burn/sparkline leaves on the one card
    that exists, and a second card in the payload with no DOM element at all.
    A crash here would have taken down the SSE listener for the entire page,
    not just the missing leaf."""
    got = _run_overview_dom(tmp_path, "snapshot", 0)
    assert got["threw"] is None, got["threw"]
    assert got["errors"] == []
    assert got["totalsToday"] == "1k"
    assert got["freshnessLabel"] == "connected"
    assert got["liveWordText"] == "busy"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_boot_with_no_data_generation_accepts_the_first_patch_frame(tmp_path):
    """Overview's freshness strip never carries `data-generation`
    (`OverviewModel` has no server generation counter), so the boot IIFE reads
    a missing attribute. `Number(null)` is 0, not NaN -- treating that 0 as a
    real generation would make a first PATCH frame that also reports
    generation 0 (a live-only tick before any full update has landed) look
    "not newer than what we already have", rejecting it and leaving the
    freshness strip stuck reporting stale/never-indexed indefinitely.
    Treating the missing attribute as unknown (not 0) accepts that frame."""
    got = _run_overview_dom(tmp_path, "patch", 0)
    assert got["threw"] is None, got["threw"]
    assert got["freshnessLabel"] == "connected", (
        "a missing data-generation on the freshness strip rejected the first "
        "patch frame as stale instead of accepting it as the baseline"
    )


# --- Hide and restore: projects.js -------------------------------------------

PROJECTS_JS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "projects.js"

# Two properties that no amount of HTML inspection reveals: which body each
# control sends, and — the one that matters — that a REFUSED hide leaves the card
# on screen. Removing it optimistically would make a 404 look exactly like a
# successful hide, and the project would vanish from a dashboard that had not
# actually hidden it. Run under node resolved by ABSOLUTE PATH: under
# `tools/falsify.py` (PATH=/usr/bin:/bin) a bare `node` would SKIP, and a skipped
# test reports SURVIVED for a mutation it would actually catch.
PROJECTS_HARNESS = """
globalThis.window = globalThis;
let clickHandler = null;

const card = { removed: false, remove() { this.removed = true; },
               querySelector: () => ({ textContent: "  demo  " }) };
const hideButton = {
  getAttribute: (n) => (n === "data-project-hide" ? "7" : null),
  closest: (sel) => (sel === "[data-project-card]" && HIDE_HAS_CARD ? card : null),
};
const restoreButton = {
  getAttribute: (n) => (n === "data-project-restore" ? "7" : null),
};
const pinButton = {
  attrs: { "data-project-pin": "7", "aria-pressed": PRESSED },
  getAttribute(n) { return this.attrs[n] ?? null; },
  setAttribute(n, v) { this.attrs[n] = v; },
};
const details = { attrs: { hidden: "" },
                  setAttribute(n, v) { this.attrs[n] = v; },
                  removeAttribute(n) { delete this.attrs[n]; } };
const count = { textContent: "0" };
const list = { appended: 0, append() { this.appended += 1; } };
const cardStatus = { textContent: "" };
const hiddenStatus = { textContent: "" };
const row = { removed: false, remove() { this.removed = true; } };

const nodes = {
  "[data-hidden-projects]": details,
  "[data-hidden-count]": count,
  "[data-hidden-list]": list,
  '[data-project-status="7"]': cardStatus,
  "[data-hidden-status]": hiddenStatus,
  '[data-hidden-project="7"]': row,
};

// Every element the client builds for a hidden row, so the test can assert
// the name is a plain span and no `/project/{id}` link is ever emitted (that
// route 404s for hidden projects).
const created = [];

// A hide on the workspace has no `[data-project-card]` ancestor; the handler
// navigates to /projects instead. `assign` records where it sent the user.
let assigned = null;
globalThis.location = { assign(target) { assigned = target; } };

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  querySelector: (sel) => nodes[sel] ?? null,
  createElement: (tag) => {
    const el = { tag, setAttribute() {}, append() {},
                 textContent: "", className: "", href: "", type: "" };
    created.push(el);
    return el;
  },
};

let sent = null;
globalThis.fetch = async (url, init) => {
  sent = { url, method: init.method, body: JSON.parse(init.body) };
  return { ok: OK, status: OK ? 200 : 404 };
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const errors = [];
console.error = (...a) => errors.push(String(a[0]));

clickHandler({ target: { closest: (sel) => {
  if (sel === "[data-project-pin]") return TARGET === "pin" ? pinButton : null;
  if (sel === "[data-project-hide]") return TARGET === "hide" ? hideButton : null;
  if (sel === "[data-project-restore]") return TARGET === "restore" ? restoreButton : null;
  return null;
} } }).then(() => {
  console.log(JSON.stringify({
    sent, errors,
    cardRemoved: card.removed,
    count: count.textContent,
    listHidden: Object.prototype.hasOwnProperty.call(details.attrs, "hidden"),
    appended: list.appended,
    cardStatus: cardStatus.textContent,
    hiddenStatus: hiddenStatus.textContent,
    rowRemoved: row.removed,
    pressed: pinButton.attrs["aria-pressed"],
    created: created.map((e) => ({ tag: e.tag, href: e.href, className: e.className })),
    assigned,
  }));
});
"""


def _run_projects(
    tmp_path, target: str, ok: bool, pressed: str = "false", hide_has_card: bool = True
) -> dict:
    harness = tmp_path / "projects_harness.js"
    harness.write_text(
        PROJECTS_HARNESS.replace("TARGET", json.dumps(target))
        .replace("PRESSED", json.dumps(pressed))
        .replace("HIDE_HAS_CARD", "true" if hide_has_card else "false")
        .replace("OK", "true" if ok else "false")
    )
    proc = subprocess.run(
        [_node(), str(harness), str(PROJECTS_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_hide_patches_the_status_and_moves_the_card_into_the_hidden_list(tmp_path):
    got = _run_projects(tmp_path, "hide", ok=True)
    assert got["sent"]["method"] == "PATCH"
    assert got["sent"]["url"] == "/api/projects/7"
    assert got["sent"]["body"] == {"status": "hidden"}
    assert got["cardRemoved"] is True
    # The list has to become reachable in the same gesture, or hiding the first
    # project strands it until a reload.
    assert got["appended"] == 1
    assert got["count"] == "1"
    assert got["listHidden"] is False, "the hidden list stayed collapsed away"
    # Never fail silently: a success announces itself, matching pin/restore.
    assert "✓" in got["cardStatus"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_hidden_row_names_the_project_in_plain_text_never_a_dead_link(tmp_path):
    """A hidden project has no workspace (`/project/{id}` 404s), so the row the
    client builds must name it in a `<span>`, not an `<a href>` -- matching the
    server-rendered hidden row in projects.html."""
    got = _run_projects(tmp_path, "hide", ok=True)
    hrefs = [e["href"] for e in got["created"]]
    assert not any("/project/" in (h or "") for h in hrefs), (
        "the client built a /project/{id} link into a hidden row -- a nav dead-end"
    )
    names = [e for e in got["created"] if e["className"] == "hidden-project__name"]
    assert names, "the hidden row has no plain-text name span"
    assert all(e["tag"] == "span" for e in names)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_hide_on_the_workspace_navigates_to_projects_rather_than_stranding(tmp_path):
    """On the workspace there is no `[data-project-card]` to fold away and a
    reload would 404, so a successful hide sends the user to /projects instead
    of leaving them on a page that no longer resolves."""
    got = _run_projects(tmp_path, "hide", ok=True, hide_has_card=False)
    assert got["assigned"] == "/projects"
    # Nothing on the workspace to fold into a hidden list, so it does not try.
    assert got["cardRemoved"] is False
    assert got["appended"] == 0


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_refused_hide_leaves_the_card_on_screen_and_says_so(tmp_path):
    """Removing it optimistically would make a 404 indistinguishable from a
    hide that worked, and the project would vanish from a dashboard that had
    not hidden it."""
    got = _run_projects(tmp_path, "hide", ok=False)
    assert got["cardRemoved"] is False
    assert got["appended"] == 0
    assert got["count"] == "0"
    assert "⚠" in got["cardStatus"]
    assert any("hiding" in e for e in got["errors"])


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_restore_patches_active_and_drops_the_row(tmp_path):
    got = _run_projects(tmp_path, "restore", ok=True)
    assert got["sent"]["body"] == {"status": "active"}
    assert got["rowRemoved"] is True
    assert got["count"] == "0"
    # The card cannot be rebuilt client-side without duplicating the template,
    # so the reload is asked for in words rather than performed.
    assert "reload" in got["hiddenStatus"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_refused_restore_leaves_the_row_in_the_list(tmp_path):
    got = _run_projects(tmp_path, "restore", ok=False)
    assert got["rowRemoved"] is False
    assert "⚠" in got["hiddenStatus"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_pin_toggles_from_the_state_it_announces(tmp_path):
    """`aria-pressed` IS the state, so the toggle reads it rather than keeping a
    second copy somewhere that could disagree with what a screen reader says."""
    on = _run_projects(tmp_path, "pin", ok=True, pressed="false")
    assert on["sent"]["body"] == {"pinned": True}, "a pin must not carry a status"
    assert on["pressed"] == "true"

    off = _run_projects(tmp_path, "pin", ok=True, pressed="true")
    assert off["sent"]["body"] == {"pinned": False}
    assert off["pressed"] == "false"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_refused_pin_leaves_the_announced_state_alone(tmp_path):
    got = _run_projects(tmp_path, "pin", ok=False, pressed="false")
    assert got["pressed"] == "false", "the button claimed a pin the server refused"
    assert "\u26a0" in got["cardStatus"]


# --- Task 2.5: projects.js -- client-side search + filter -------------------
#
# The server renders the full stable list; this harness proves the filtering
# itself never reloads and never fetches -- `fetch` is stubbed only to record
# whether it was ever called, and every scenario below asserts it was not.
# `[data-project-row-item]` rows and `[data-projects-filter]` buttons are
# plain JS objects (no real DOM), matching this file's existing style rather
# than adding a DOM library dependency.

PROJECTS_FILTER_HARNESS = """
globalThis.window = globalThis;
let clickHandler = null;
let inputHandler = null;
let fetchCalled = false;
globalThis.fetch = async () => { fetchCalled = true; return { ok: true }; };

function makeFilterButton(name, pressed) {
  return {
    attrs: { "data-projects-filter": name, "aria-pressed": pressed },
    getAttribute(n) { return this.attrs[n] ?? null; },
    setAttribute(n, v) { this.attrs[n] = v; },
  };
}
const filterAll = makeFilterButton("all", "true");
const filterAttention = makeFilterButton("needs_attention", "false");
const filterRunning = makeFilterButton("running", "false");
const filterQueued = makeFilterButton("queued", "false");
const filterHidden = makeFilterButton("hidden", "false");
const filterButtons = [filterAll, filterAttention, filterRunning, filterQueued, filterHidden];

function makeRow(name, path, state) {
  return {
    hidden: false,
    attrs: { "data-project-name": name, "data-project-path": path, "data-project-state": state },
    getAttribute(n) { return this.attrs[n] ?? null; },
  };
}
// queued, running, idle -- deliberately no "stale" row: needs_attention's
// three-state union is exercised by queued+running alone, and this keeps the
// fixture small.
const rowAlpha = makeRow("Alpha Project", "/x/alpha", "queued");
const rowBeta = makeRow("Beta", "/x/beta-path", "running");
const rowGamma = makeRow("Gamma", "/x/gamma", "idle");
const rows = [rowAlpha, rowBeta, rowGamma];

const hiddenRows = [{ hidden: false, textContent: "Zeta hidden-project" }];

const searchInput = { value: "" };
const countNode = { textContent: "" };
const emptyNode = { hidden: true };
const listNode = { hidden: false };
const hiddenSection = { hidden: true };

globalThis.document = {
  addEventListener(type, fn) {
    if (type === "click") clickHandler = fn;
    if (type === "input") inputHandler = fn;
  },
  querySelector(sel) {
    if (sel === "[data-projects-list]") return listNode;
    if (sel === "[data-projects-search]") return searchInput;
    if (sel === "[data-projects-count]") return countNode;
    if (sel === "[data-projects-empty]") return emptyNode;
    if (sel === "[data-hidden-projects]") return hiddenSection;
    if (sel === '[data-projects-filter][aria-pressed="true"]') {
      return filterButtons.find((b) => b.attrs["aria-pressed"] === "true") || null;
    }
    return null;
  },
  querySelectorAll(sel) {
    if (sel === "[data-project-row-item]") return rows;
    if (sel === "[data-projects-filter]") return filterButtons;
    if (sel === "[data-hidden-project]") return hiddenRows;
    return [];
  },
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

// Simulate typing into the search field, then (optionally) clicking a filter
// button -- exactly the two entry points `applyProjectsFilter` has.
searchInput.value = QUERY;
inputHandler({ target: { closest: (sel) => (sel === "[data-projects-search]" ? searchInput : null) } });

if (FILTER_TARGET) {
  const target = filterButtons.find((b) => b.attrs["data-projects-filter"] === FILTER_TARGET);
  clickHandler({ target: { closest: (sel) => (sel === "[data-projects-filter]" ? target : null) } });
}

console.log(JSON.stringify({
  rowHidden: rows.map((r) => r.hidden),
  hiddenRowHidden: hiddenRows.map((r) => r.hidden),
  count: countNode.textContent,
  emptyHidden: emptyNode.hidden,
  listHidden: listNode.hidden,
  hiddenSectionHidden: hiddenSection.hidden,
  pressed: filterButtons.map((b) => b.attrs["aria-pressed"]),
  fetchCalled,
}));
"""


def _run_projects_filter(tmp_path, query: str, filter_target):
    harness = tmp_path / "projects_filter_harness.js"
    target_literal = json.dumps(filter_target) if filter_target is not None else "null"
    harness.write_text(
        PROJECTS_FILTER_HARNESS.replace("QUERY", json.dumps(query))
        .replace("FILTER_TARGET", target_literal)
    )
    proc = subprocess.run(
        [_node(), str(harness), str(PROJECTS_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_projects_search_hides_rows_not_matching_name_or_path(tmp_path):
    got = _run_projects_filter(tmp_path, "beta", None)
    assert got["rowHidden"] == [True, False, True]
    assert got["count"] == "1 project shown"
    assert got["emptyHidden"] is True
    assert got["fetchCalled"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_projects_search_matches_the_path_as_well_as_the_name(tmp_path):
    got = _run_projects_filter(tmp_path, "beta-path", None)
    assert got["rowHidden"] == [True, False, True]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_projects_filter_running_combines_with_the_search(tmp_path):
    got = _run_projects_filter(tmp_path, "", "running")
    assert got["rowHidden"] == [True, False, True]
    assert got["count"] == "1 project shown"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_projects_filter_needs_attention_covers_queued_and_running(tmp_path):
    got = _run_projects_filter(tmp_path, "", "needs_attention")
    assert got["rowHidden"] == [False, False, True]
    assert got["count"] == "2 projects shown"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_projects_search_with_no_match_shows_the_empty_state(tmp_path):
    got = _run_projects_filter(tmp_path, "zzz-nope", None)
    assert got["rowHidden"] == [True, True, True]
    assert got["count"] == "0 projects shown"
    assert got["emptyHidden"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_projects_hidden_filter_reveals_the_hidden_section_with_no_fetch(tmp_path):
    got = _run_projects_filter(tmp_path, "", "hidden")
    assert got["listHidden"] is True
    assert got["hiddenSectionHidden"] is False
    assert got["hiddenRowHidden"] == [False]
    assert got["pressed"] == ["false", "false", "false", "false", "true"]
    assert got["fetchCalled"] is False



# --- Task 5: schedule.js — the datetime<->epoch math and the click flows ----
#
# The highest-risk code in Task 5: a timezone or seconds/milliseconds mistake
# here schedules a session at the wrong time with no test anywhere to catch
# it. Every subprocess pins `TZ=UTC` so `Date`'s local-time behaviour (which
# `localInputToEpoch`/`epochToDatetimeLocalValue` both lean on) is the same on
# every machine this runs on, not just the author's.

SCHEDULE_JS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "schedule.js"

UTC_ENV = {**os.environ, "TZ": "UTC"}


def _run_node(tmp_path, name: str, script: str, target: Path) -> dict:
    harness = tmp_path / name
    harness.write_text(script)
    proc = subprocess.run(
        [_node(), str(harness), str(target)],
        capture_output=True, text=True, env=UTC_ENV,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


PURE_MATH_HARNESS = """
globalThis.window = globalThis;
globalThis.document = { addEventListener() {} };
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));
console.log(JSON.stringify({
  // A `Math.round` regression would make these two disagree: rounding up
  // ".999" of a second crosses into the next whole second, floor does not.
  onTheSecond: window.localInputToEpoch("2026-06-01T10:00:00.000"),
  flooredNotRounded: window.localInputToEpoch("2026-06-01T10:00:00.999"),
  // No seconds at all -- the actual precision a real <input type=
  // "datetime-local"> ever produces.
  minutePrecision: window.localInputToEpoch("2026-06-01T10:00"),
  roundTrip: window.localInputToEpoch(
    window.epochToDatetimeLocalValue(1780308000)) === 1780308000,
  epoch: window.epochToDatetimeLocalValue(0),
  empty: window.localInputToEpoch(""),
}));
"""


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_local_input_to_epoch_floors_seconds_rather_than_rounding(tmp_path):
    """(a) A `Math.round`/milliseconds regression must fail this."""
    got = _run_node(tmp_path, "pure_math.js", PURE_MATH_HARNESS, SCHEDULE_JS)
    assert got["onTheSecond"] == got["flooredNotRounded"], (
        "flooring must land on the SAME second regardless of the fractional "
        "remainder -- rounding would push .999 into the next second"
    )
    assert got["minutePrecision"] == 1780308000, (
        "2026-06-01T10:00:00 UTC, the only precision a real datetime-local "
        "input ever produces"
    )
    assert got["empty"] is None


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_epoch_to_datetime_local_value_round_trips(tmp_path):
    """(b) Formats a known epoch to its wall-clock value and back."""
    got = _run_node(tmp_path, "pure_math.js", PURE_MATH_HARNESS, SCHEDULE_JS)
    assert got["epoch"] == "1970-01-01T00:00"
    assert got["roundTrip"] is True


# --- The compose Run now path shares the launch band's settings/recovery ----

COMPOSE_RUN_HARNESS = """
globalThis.window = globalThis;
let clickHandler = null;
const promptField = { value: "typed prompt" };
const statusNode = { textContent: "" };
let launchOptionsArgs = null;
let copied = null;
let sentBody = null;

window.bridgeLaunchBody = (id, path) => {
  launchOptionsArgs = [id, path];
  return {
    project_path: path,
    mode: "terminal",
    model: "claude-opus-4-8",
    effort: "xhigh",
    permission_mode: "bypassPermissions",
  };
};
window.bridgeCopy = async (text) => {
  copied = text;
  return "✓ Copied to clipboard";
};

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  getElementById: (id) => id === "compose-1" ? promptField : null,
  querySelector: (sel) =>
    sel === '[data-compose-status="compose-1"]' ? statusNode : null,
};

globalThis.fetch = async (_url, init) => {
  sentBody = JSON.parse(init.body);
  FETCH_BEHAVIOR
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const button = {
  disabled: false,
  setAttribute() {},
  removeAttribute() {},
  getAttribute(name) {
    if (name === "data-compose-run") return "compose-1";
    if (name === "data-compose-path") return "/Users/mitsheth/dev/demo";
    if (name === "data-compose-launch") return "launch-1";
    return null;
  },
  closest(sel) { return sel === "[data-compose-run]" ? button : null; },
};

clickHandler({ target: button }).then(() => {
  console.log(JSON.stringify({
    launchOptionsArgs, copied, sentBody,
    prompt: promptField.value,
    status: statusNode.textContent,
  }));
});
"""


def _run_compose_now(tmp_path, behavior: str) -> dict:
    behaviors = {
        "started": 'return { ok: true, status: 200, json: async () => ({ outcome: "started" }) };',
        "refused": 'return { ok: false, status: 400, json: async () => ({ detail: "refused" }) };',
        "failed": 'return { ok: true, status: 200, json: async () => ({ outcome: "failed", error: "spawn failed" }) };',
        "network": 'throw new Error("offline");',
    }
    script = COMPOSE_RUN_HARNESS.replace("FETCH_BEHAVIOR", behaviors[behavior])
    return _run_node(tmp_path, f"compose_{behavior}.js", script, SCHEDULE_JS)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_compose_run_now_uses_the_card_launch_settings(tmp_path):
    got = _run_compose_now(tmp_path, "started")
    assert got["launchOptionsArgs"] == [
        "launch-1", "/Users/mitsheth/dev/demo",
    ]
    assert got["sentBody"] == {
        "project_path": "/Users/mitsheth/dev/demo",
        "prompt": "typed prompt",
        "mode": "terminal",
        "model": "claude-opus-4-8",
        "effort": "xhigh",
        "permission_mode": "bypassPermissions",
    }
    assert got["prompt"] == ""
    assert got["copied"] is None


@pytest.mark.skipif(_node() is None, reason="node is not installed")
@pytest.mark.parametrize("behavior", ["refused", "failed", "network"])
def test_compose_run_now_copies_and_preserves_the_prompt_on_every_failure(
    tmp_path, behavior,
):
    got = _run_compose_now(tmp_path, behavior)
    assert got["copied"] == "typed prompt"
    assert got["prompt"] == "typed prompt"
    assert got["status"].startswith("⚠ Launch failed — prompt copied")


# --- (c) The schedule-submit handler: seconds, and source_handoff_id --------

SCHEDULE_SUBMIT_HARNESS = """
globalThis.window = globalThis;
let clickHandler = null;

const promptField = { value: "do the thing" };
const whenInput = { value: "2026-06-01T10:00" };
const modeSelect = { value: "background" };
const toggleButton = { focus() {}, setAttribute() {}, getAttribute: () => null };
const summaryCount = { textContent: "0" };
const topbarCount = { textContent: "0" };

const panel = {
  hidden: false,
  getAttribute(name) {
    if (name === "data-schedule-path") return "/Users/mitsheth/dev/demo";
    if (name === "data-schedule-prompt") return "compose-1";
    if (name === "data-schedule-handoff") return HANDOFF_ID;
    return null;
  },
  querySelector(sel) {
    if (sel === "[data-schedule-when]") return whenInput;
    if (sel === "[data-schedule-mode]") return modeSelect;
    return null;
  },
};

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  getElementById: (id) => {
    if (id === "schedule-panel-1") return panel;
    if (id === "compose-1") return promptField;
    return null;
  },
  querySelector(sel) {
    if (sel === '[data-schedule-toggle="schedule-panel-1"]') return toggleButton;
    if (sel === "[data-scheduled-count]") return summaryCount;
    if (sel === "[data-topbar-scheduled]") return topbarCount;
    return null;
  },
};

let sentUrl = null;
let sentMethod = null;
let sentBody = null;
globalThis.fetch = async (url, init) => {
  sentUrl = url;
  sentMethod = init.method;
  sentBody = JSON.parse(init.body);
  return { ok: true, status: 201, json: async () => ({ id: "new-job" }) };
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const submitButton = {
  getAttribute: () => "schedule-panel-1",
  closest: (sel) => (sel === "[data-schedule-submit]" ? submitButton : null),
  disabled: false,
};

clickHandler({ target: submitButton }).then(() => {
  console.log(JSON.stringify({ sentUrl, sentMethod, sentBody }));
});
"""


def _run_schedule_submit(tmp_path, handoff_id) -> dict:
    script = SCHEDULE_SUBMIT_HARNESS.replace("HANDOFF_ID", json.dumps(handoff_id))
    return _run_node(tmp_path, "schedule_submit.js", script, SCHEDULE_JS)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_schedule_submit_posts_seconds_and_a_null_source_for_the_compose_box(tmp_path):
    got = _run_schedule_submit(tmp_path, handoff_id=None)
    assert got["sentUrl"] == "/api/schedule"
    assert got["sentMethod"] == "POST"
    body = got["sentBody"]
    assert body["project_path"] == "/Users/mitsheth/dev/demo"
    assert body["prompt"] == "do the thing"
    assert body["mode"] == "background"
    # Seconds, not milliseconds: `new Date().getTime()` is milliseconds, and a
    # regression that dropped the `/ 1000` would schedule 1000x too far out.
    assert body["scheduled_for"] == 1780308000
    assert body["source_handoff_id"] is None


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_schedule_submit_carries_the_handoff_id_for_the_handoff_path(tmp_path):
    got = _run_schedule_submit(tmp_path, handoff_id="h1")
    assert got["sentBody"]["source_handoff_id"] == "h1"


# --- (d) The Scheduled section's cancel handler -----------------------------

SCHEDULE_CANCEL_HARNESS = """
globalThis.window = globalThis;
let clickHandler = null;

const row = { removed: false, remove() { this.removed = true; } };
const summary = { focused: false, focus() { this.focused = true; } };
const sectionStatus = { textContent: "" };
const rowStatus = { textContent: "" };
const summaryCount = { textContent: "2" };
const topbarCount = { textContent: "2" };

const nodes = {
  '[data-scheduled-job="sched-9"]': row,
  "[data-scheduled] summary": summary,
  "[data-scheduled-section-status]": sectionStatus,
  '[data-scheduled-status="sched-9"]': rowStatus,
  "[data-scheduled-count]": summaryCount,
  "[data-topbar-scheduled]": topbarCount,
  "[data-scheduled]": null,
};

const list = { children: { length: REMAINING } };
const details = { hidden: false, open: true };
nodes["[data-scheduled-list]"] = list;
nodes["[data-scheduled]"] = details;

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  querySelector: (sel) => nodes[sel] ?? null,
};

let sentUrl = null;
let sentMethod = null;
globalThis.fetch = async (url, init) => {
  sentUrl = url;
  sentMethod = init.method;
  return { ok: true, status: 200, json: async () => ({}) };
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const cancelButton = {
  getAttribute: () => "sched-9",
  closest: (sel) => (sel === "[data-scheduled-cancel]" ? cancelButton : null),
  disabled: false,
};

clickHandler({ target: cancelButton }).then(() => {
  console.log(JSON.stringify({
    sentUrl, sentMethod,
    rowRemoved: row.removed,
    summaryFocused: summary.focused,
    sectionStatus: sectionStatus.textContent,
    count: summaryCount.textContent,
    topbarCount: topbarCount.textContent,
    detailsHidden: details.hidden,
    detailsOpen: details.open,
  }));
});
"""


def _run_schedule_cancel(tmp_path, remaining: int = 1) -> dict:
    """`remaining` is how many rows are left in the list AFTER the cancel."""
    return _run_node(
        tmp_path, "schedule_cancel.js",
        SCHEDULE_CANCEL_HARNESS.replace("REMAINING", str(remaining)), SCHEDULE_JS,
    )


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_scheduled_cancel_deletes_the_right_job(tmp_path):
    got = _run_schedule_cancel(tmp_path)
    assert got["sentUrl"] == "/api/schedule/sched-9"
    assert got["sentMethod"] == "DELETE"
    assert got["rowRemoved"] is True


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_scheduled_cancel_moves_focus_and_announces_before_the_row_is_gone(tmp_path):
    """WCAG 2.4.3/4.1.3: the row (and its own status span) is removed in the
    same tick, so both the focus target and the announcement have to live
    somewhere that outlives it."""
    got = _run_schedule_cancel(tmp_path)
    assert got["summaryFocused"] is True
    assert "Cancelled" in got["sectionStatus"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_scheduled_cancel_decrements_both_stale_counts(tmp_path):
    """The topbar count and the section's own summary count are rendered
    once, on load. Without this fix they read "2" forever after a cancel."""
    got = _run_schedule_cancel(tmp_path)
    assert got["count"] == "1"
    assert got["topbarCount"] == "1"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_cancelling_the_last_row_closes_the_empty_section(tmp_path):
    """An open <details> reading "Scheduled 0" with nothing under it is what
    the count-only bookkeeping used to leave behind."""
    got = _run_schedule_cancel(tmp_path, remaining=0)
    assert got["detailsOpen"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_the_emptied_section_is_collapsed_but_never_hidden(tmp_path):
    """Collapsing is safe; hiding is not. The cancel handler has just moved
    focus to this <summary> and announced "Cancelled" into a live region
    inside it -- `hidden` takes both out of the accessibility tree in the same
    task, dropping focus to <body> and swallowing the announcement (WCAG
    2.4.3, 4.1.3)."""
    got = _run_schedule_cancel(tmp_path, remaining=0)
    assert got["detailsHidden"] is False
    assert got["summaryFocused"] is True
    assert "Cancelled" in got["sectionStatus"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_cancel_that_leaves_a_row_behind_keeps_the_section_open(tmp_path):
    """A finished run still on screen is history worth reading -- the count
    says zero ACTIVE jobs, which is not the same as an empty list."""
    got = _run_schedule_cancel(tmp_path, remaining=1)
    assert got["detailsHidden"] is False
    assert got["detailsOpen"] is True


# --- (e) run-now leaves a row the server would recognise --------------------
#
# The row is server-rendered once, with the controls a `pending` job gets. After
# a run-now it is terminal, and every one of those controls can now only 409 --
# so until a reload the panel was showing buttons that could not work, and a
# `failed` run showed no Retry at all.

SETTLE_HARNESS = """
globalThis.window = globalThis;
let clickHandler = null;

const state = { textContent: "pending" };
const announceSpan = { name: "announce" };
const controls = [
  { removed: false, remove() { this.removed = true; } },
  { removed: false, remove() { this.removed = true; } },
  { removed: false, remove() { this.removed = true; } },
];
let queriedAll = null;
const inserted = [];
const row = {
  className: "scheduled__job scheduled__job--pending",
  getAttribute: (n) =>
    (n === "data-scheduled-retry-label" ? "Retry demo run scheduled for T" : null),
  querySelector(sel) {
    if (sel === "[data-scheduled-state]") return state;
    if (sel === "[data-scheduled-status]") return announceSpan;
    if (sel === "[data-scheduled-error]") return this.note ?? null;
    return null;
  },
  querySelectorAll(sel) { queriedAll = sel; return controls; },
  insertBefore(el, before) {
    inserted.push({ el, before: before ? before.name ?? "note" : null });
    if (el.attrs && el.attrs["data-scheduled-error"] !== undefined) this.note = el;
  },
};
const rowStatus = { textContent: "" };
const summaryCount = { textContent: "1" };
const topbarCount = { textContent: "1" };
const summary = { focused: false, focus() { this.focused = true; } };

const nodes = {
  '[data-scheduled-job="sched-7"]': row,
  '[data-scheduled-status="sched-7"]': rowStatus,
  "[data-scheduled-count]": summaryCount,
  "[data-topbar-scheduled]": topbarCount,
  "[data-scheduled-list]": { children: { length: 1 } },
  "[data-scheduled]": { hidden: false, open: true },
  "[data-scheduled] summary": summary,
};

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  querySelector: (sel) => nodes[sel] ?? null,
  createElement: () => ({
    attrs: {}, setAttribute(n, v) { this.attrs[n] = v; },
    focused: false, focus() { this.focused = true; },
    textContent: "", className: "", type: "",
  }),
};

globalThis.fetch = async () => ({
  ok: true, status: 200,
  json: async () => ({ id: "sched-7", status: STATUS, error: "spawn boom" }),
});

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const runNowButton = {
  getAttribute: () => "sched-7",
  closest: (sel) => (sel === "[data-scheduled-run-now]" ? runNowButton : null),
  disabled: false,
};

clickHandler({ target: runNowButton }).then(() => {
  console.log(JSON.stringify({
    rowClass: row.className,
    state: state.textContent,
    queriedAll,
    removed: controls.map((c) => c.removed),
    inserted: inserted.map((i) => ({
      text: i.el.textContent, attrs: i.el.attrs, before: i.before,
      focused: i.el.focused ?? null, className: i.el.className,
    })),
    rowStatus: rowStatus.textContent,
    count: summaryCount.textContent,
    summaryFocused: summary.focused,
  }));
});
"""


def _run_settle(tmp_path, status: str) -> dict:
    return _run_node(
        tmp_path, "schedule_settle.js",
        SETTLE_HARNESS.replace("STATUS", json.dumps(status)), SCHEDULE_JS,
    )


def _buttons(got):
    return [i for i in got["inserted"] if i["text"] == "Retry"]


def _notes(got):
    return [i for i in got["inserted"] if i["className"] == "card__note"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_run_now_restates_the_row_and_drops_the_pending_only_controls(tmp_path):
    got = _run_settle(tmp_path, "fired")
    assert got["rowClass"] == "scheduled__job scheduled__job--fired"
    assert got["state"] == "fired"
    assert all(got["removed"]), "a pending-only control survived the run"
    for control in ("run-now", "edit-toggle", "cancel", "edit-panel"):
        assert f"data-scheduled-{control}" in got["queriedAll"]
    assert _buttons(got) == [], "a fired run has nothing to retry"
    assert got["count"] == "0"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_fired_run_now_hands_focus_to_the_section_it_leaves_behind(tmp_path):
    """The clicked button was just removed. Nothing replaced it on a `fired`
    row, so focus has to land somewhere that outlives the row rather than
    silently falling to <body> (WCAG 2.4.3)."""
    got = _run_settle(tmp_path, "fired")
    assert got["summaryFocused"] is True


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_run_now_that_fails_grows_a_focused_retry_button_naming_its_own_job(tmp_path):
    got = _run_settle(tmp_path, "failed")
    buttons = _buttons(got)
    assert len(buttons) == 1
    button = buttons[0]
    assert button["attrs"]["data-scheduled-retry"] == "sched-7"
    # Named, not a bare "Retry": twenty finished rows would otherwise be twenty
    # identical entries in a screen reader's button list.
    assert button["attrs"]["aria-label"] == "Retry demo run scheduled for T"
    # Focus follows the control that replaced the one just removed, and stays
    # in the row rather than jumping to the section.
    assert button["focused"] is True and got["summaryFocused"] is False
    assert "spawn boom" in got["rowStatus"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_failed_run_now_keeps_the_error_on_screen_not_just_in_the_live_region(tmp_path):
    """A polite announcement is gone the moment anything else is announced. A
    row marked "failed" whose reason exists nowhere on the page is what this
    note prevents -- the server renders one, so a settled row must too."""
    got = _run_settle(tmp_path, "failed")
    notes = _notes(got)
    assert len(notes) == 1
    assert notes[0]["attrs"]["data-scheduled-error"] == ""


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_the_settled_retry_button_lands_before_the_live_region(tmp_path):
    """Server order is Retry, then the error note, then the announce span. A
    button appended after the live region would tab differently depending on
    whether the row was reloaded or settled in place."""
    got = _run_settle(tmp_path, "failed")
    assert _buttons(got)[0]["before"] == "note", (
        "the retry button must precede the error note the server puts after it"
    )
    assert _notes(got)[0]["before"] == "announce"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_an_indeterminate_run_now_also_gets_a_retry_button(tmp_path):
    """`run-now` is gated to `pending` and the scheduler never re-claims an
    indeterminate row, so this button is the only recovery it has."""
    got = _run_settle(tmp_path, "indeterminate")
    assert _buttons(got)[0]["attrs"]["data-scheduled-retry"] == "sched-7"


# --- (f) retry goes to the schedule, not to a bare launch -------------------

RETRY_HARNESS = """
globalThis.window = globalThis;
let clickHandler = null;

const rowStatus = { textContent: "" };
const state = { textContent: "failed" };
const announceSpan = {};
const inserted = [];
const row = {
  className: "scheduled__job scheduled__job--failed",
  getAttribute: () => "sched-5",
  querySelector(sel) {
    if (sel === "[data-scheduled-state]") return state;
    if (sel === "[data-scheduled-status]") return announceSpan;
    if (sel === "[data-scheduled-error]") return this.note ?? null;
    return null;
  },
  insertBefore(el) { inserted.push(el); this.note = el; },
};
const nodes = { '[data-scheduled-status="sched-5"]': rowStatus };

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  querySelector: (sel) => nodes[sel] ?? null,
  createElement: () => ({
    attrs: {}, setAttribute(n, v) { this.attrs[n] = v; },
    textContent: "", className: "",
  }),
};

let sentUrl = null;
let sentMethod = null;
let sentBody = null;
globalThis.fetch = async (url, init) => {
  sentUrl = url;
  sentMethod = init.method;
  sentBody = init.body ?? null;
  return { ok: OK, status: STATUS_CODE, json: async () => (RESPONSE) };
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const retryButton = {
  attrs: { "data-scheduled-retry": "sched-5" },
  getAttribute(n) { return this.attrs[n] ?? null; },
  setAttribute(n, v) { this.attrs[n] = v; },
  removed: false,
  remove() { this.removed = true; },
  closest(sel) {
    if (sel === "[data-scheduled-retry]") return this;
    if (sel === "[data-scheduled-job]") return row;
    return null;
  },
  disabled: false,
};

clickHandler({ target: retryButton }).then(() => {
  console.log(JSON.stringify({
    sentUrl, sentMethod, sentBody,
    rowStatus: rowStatus.textContent,
    buttonRemoved: retryButton.removed,
    retryTarget: retryButton.attrs["data-scheduled-retry"],
    rowClass: row.className,
    state: state.textContent,
    note: inserted.map((el) => el.textContent),
  }));
});
"""


def _run_retry(tmp_path, response: dict, ok: bool = True, code: int = 200) -> dict:
    script = (RETRY_HARNESS
              .replace("RESPONSE", json.dumps(response))
              .replace("STATUS_CODE", str(code))
              .replace("OK", "true" if ok else "false"))
    return _run_node(tmp_path, "schedule_retry.js", script, SCHEDULE_JS)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_retry_posts_to_the_schedule_endpoint_and_carries_no_prompt(tmp_path):
    """The bug this replaced: retrying through `/api/launch` sent the prompt
    scraped from the page and no `handoff_id`, so a schedule created from a
    handoff was retried successfully while its handoff stayed queued. The
    server owns all of it now, which is why the request has no body at all.
    """
    got = _run_retry(tmp_path, {"id": "sched-6", "status": "fired"})
    assert got["sentUrl"] == "/api/schedule/sched-5/retry"
    assert got["sentMethod"] == "POST"
    assert got["sentBody"] is None
    assert "Retried" in got["rowStatus"]
    assert got["buttonRemoved"] is True, "the server allows exactly one retry"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_retry_that_fails_again_repoints_the_button_at_the_new_run(tmp_path):
    """One retry per row, so a second click on the ORIGINAL id can only 409.
    The failure a person is now looking at is the retry's, and that is the row
    the button has to name for the recovery loop to keep working."""
    got = _run_retry(tmp_path, {"id": "sched-6", "status": "failed",
                                "error": "still no claude"})
    assert got["retryTarget"] == "sched-6"
    assert got["buttonRemoved"] is False
    assert "still no claude" in got["rowStatus"]
    # ...and the row is restated to match, so it never describes run A while
    # its only control acts on run B.
    assert got["note"] == ["still no claude"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_chained_retry_restates_the_row_without_growing_a_second_button(tmp_path):
    got = _run_retry(tmp_path, {"id": "sched-6", "status": "indeterminate"})
    assert got["state"] == "indeterminate"
    assert got["rowClass"] == "scheduled__job scheduled__job--indeterminate"
    assert got["note"] == [], "no error, so no empty note"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_refused_retry_removes_a_button_that_could_only_ever_409(tmp_path):
    got = _run_retry(tmp_path, {"detail": "only a failed or indeterminate run "
                                          "can be retried, once"},
                     ok=False, code=409)
    assert got["buttonRemoved"] is True
    assert "⚠" in got["rowStatus"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_retry_refused_for_any_other_reason_keeps_the_button(tmp_path):
    """A 500 is not a permanent no -- removing the control would strand the
    row with no way to try again."""
    got = _run_retry(tmp_path, {"detail": "boom"}, ok=False, code=500)
    assert got["buttonRemoved"] is False
    assert got["retryTarget"] == "sched-5"


# --- Task 1.3: shell.js — the Menu disclosure toggles the sidebar nav -------
#
# Nothing here exercises real CSS, so this cannot prove the nav is visually
# hidden at narrow widths -- only that the JS which drives the disclosure
# does not run until a click, and toggles both halves of the ARIA contract
# (the button's `aria-expanded` and the nav's `hidden` attribute) in lockstep.

SHELL_JS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "shell.js"

SHELL_HARNESS = """
globalThis.window = globalThis;
const nav = {
  attrs: {},
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); },
  setAttribute(name, value) { this.attrs[name] = value; },
  removeAttribute(name) { delete this.attrs[name]; },
};
const button = {
  attrs: { "aria-expanded": "true", "aria-controls": "primary-nav" },
  getAttribute(name) { return this.attrs[name] ?? null; },
  setAttribute(name, value) { this.attrs[name] = value; },
  closest(sel) { return sel === ".menu-toggle" ? this : null; },
};
let clickHandler = null;
globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  getElementById: (id) => (id === "primary-nav" ? nav : null),
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const beforeExpanded = button.attrs["aria-expanded"];
const beforeHidden = nav.hasAttribute("hidden");

clickHandler({ target: button });
const afterFirstClickExpanded = button.attrs["aria-expanded"];
const afterFirstClickHidden = nav.hasAttribute("hidden");

clickHandler({ target: button });
const afterSecondClickExpanded = button.attrs["aria-expanded"];
const afterSecondClickHidden = nav.hasAttribute("hidden");

console.log(JSON.stringify({
  beforeExpanded, beforeHidden,
  afterFirstClickExpanded, afterFirstClickHidden,
  afterSecondClickExpanded, afterSecondClickHidden,
}));
"""


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_shell_js_does_nothing_on_load(tmp_path):
    """With no JS -- or before any click -- the nav is exactly as the server
    rendered it: visible, `hidden` absent. Loading shell.js must not itself
    collapse the nav; only a click may."""
    harness = tmp_path / "shell_load_harness.js"
    harness.write_text(SHELL_HARNESS)
    proc = subprocess.run(
        [_node(), str(harness), str(SHELL_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["beforeHidden"] is False
    assert got["beforeExpanded"] == "true"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_shell_js_menu_toggle_collapses_and_restores_the_nav(tmp_path):
    harness = tmp_path / "shell_toggle_harness.js"
    harness.write_text(SHELL_HARNESS)
    proc = subprocess.run(
        [_node(), str(harness), str(SHELL_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["afterFirstClickExpanded"] == "false"
    assert got["afterFirstClickHidden"] is True
    assert got["afterSecondClickExpanded"] == "true"
    assert got["afterSecondClickHidden"] is False


# --- Task 5.2: settings.js — theme/density apply + the never-arms-launch seam

SETTINGS_JS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "settings.js"

# `settings.js` runs top-level (no wrapper) on load, exactly like launch.js:
# it applies theme/density immediately, registers a matchMedia "change"
# listener, then binds each `data-settings-*` select. The harness stubs just
# enough of `document`/`localStorage`/`matchMedia` to observe all of that, plus
# an `__INTERACTIONS__` placeholder for post-load steps a specific test needs
# (firing a select's "change" listener, or a simulated OS theme change).
SETTINGS_HARNESS = """
globalThis.window = globalThis;

let prefersDark = __PREFERS_DARK__;
const changeHandlers = [];
globalThis.matchMedia = () => ({
  matches: prefersDark,
  addEventListener(type, fn) { if (type === "change") changeHandlers.push(fn); },
});

const store = __STORAGE__;
const setCalls = [];
globalThis.localStorage = {
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
  },
  setItem(key, value) { store[key] = value; setCalls.push([key, value]); },
  removeItem(key) { delete store[key]; },
};

const themeSetCalls = [];
const documentElement = {
  attrs: {},
  setAttribute(name, value) {
    this.attrs[name] = value;
    if (name === "data-theme") themeSetCalls.push(value);
  },
  removeAttribute(name) { delete this.attrs[name]; },
  getAttribute(name) { return this.attrs[name] ?? null; },
};

function makeSelect(initialValue) {
  return {
    value: initialValue,
    listeners: [],
    addEventListener(type, fn) { if (type === "change") this.listeners.push(fn); },
  };
}

const selects = {
  '[data-settings-theme]': makeSelect("system"),
  '[data-settings-density]': makeSelect("comfortable"),
  '[data-settings-launch-model]': makeSelect("opus"),
  '[data-settings-launch-effort]': makeSelect("low"),
  '[data-settings-launch-mode]': makeSelect(""),
  // Present in the harness so a mistaken query for the LIVE launch band's
  // permission control would resolve to something observable instead of
  // silently returning null and masking the bug.
  '[data-launch-perm="launch-1"]': makeSelect("bypassPermissions"),
};

const queried = [];
globalThis.document = {
  documentElement,
  querySelector(sel) { queried.push(sel); return selects[sel] ?? null; },
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

__INTERACTIONS__

console.log(JSON.stringify({
  themeAttr: documentElement.attrs["data-theme"] ?? null,
  densityAttr: documentElement.attrs["data-density"] ?? null,
  hasDensityAttr: Object.prototype.hasOwnProperty.call(documentElement.attrs, "data-density"),
  setCalls,
  queried,
  changeHandlersCount: changeHandlers.length,
  themeSetCalls,
  selectValues: Object.fromEntries(Object.entries(selects).map(([k, v]) => [k, v.value])),
}));
"""


def _run_settings_harness(
    tmp_path, storage: dict, prefers_dark: bool = False, interactions: str = "",
) -> dict:
    harness = tmp_path / "settings_harness.js"
    text = (
        SETTINGS_HARNESS
        .replace("__STORAGE__", json.dumps(storage))
        .replace("__PREFERS_DARK__", "true" if prefers_dark else "false")
        .replace("__INTERACTIONS__", interactions)
    )
    harness.write_text(text)
    proc = subprocess.run(
        [_node(), str(harness), str(SETTINGS_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_settings_js_applies_a_stored_theme_and_density_on_load(tmp_path):
    got = _run_settings_harness(
        tmp_path, {"bridge.appearance": "dark", "bridge.density": "compact"},
    )
    assert got["themeAttr"] == "dark"
    assert got["densityAttr"] == "compact"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_settings_js_comfortable_density_leaves_the_attribute_unset(tmp_path):
    got = _run_settings_harness(tmp_path, {"bridge.density": "comfortable"})
    assert got["hasDensityAttr"] is False


@pytest.mark.parametrize("prefers_dark,expected", [(True, "dark"), (False, "light")])
@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_settings_js_system_theme_follows_the_os_preference(tmp_path, prefers_dark, expected):
    got = _run_settings_harness(tmp_path, {}, prefers_dark=prefers_dark)
    assert got["themeAttr"] == expected


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_settings_js_system_theme_updates_live_when_the_os_changes(tmp_path):
    """No reload: a change event fired after load must be enough to flip the
    already-applied theme, proving the matchMedia listener actually re-applies
    rather than only reading the OS preference once at startup."""
    got = _run_settings_harness(
        tmp_path, {"bridge.appearance": "system"}, prefers_dark=False,
        interactions="prefersDark = true; changeHandlers.forEach((fn) => fn());",
    )
    assert got["themeAttr"] == "dark"
    assert got["changeHandlersCount"] >= 1


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_settings_js_an_explicit_theme_choice_ignores_a_later_os_change(tmp_path):
    """Only "System" is live -- a user who picked Light must not be silently
    flipped to Dark just because the OS changed. The resulting value would be
    "light" either way (an explicit choice short-circuits before matchMedia is
    even consulted), so the guard is only observable through whether it
    re-applies AT ALL: the change handler must be a no-op for an explicit
    choice, not just a same-value one."""
    got = _run_settings_harness(
        tmp_path, {"bridge.appearance": "light"}, prefers_dark=False,
        interactions="prefersDark = true; changeHandlers.forEach((fn) => fn());",
    )
    assert got["themeAttr"] == "light"
    # Exactly the one call from the initial `applyTheme()` at load -- the OS
    # change interaction above must not have re-applied at all.
    assert got["themeSetCalls"] == ["light"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_settings_js_restores_stored_launch_defaults_into_their_own_selects(tmp_path):
    got = _run_settings_harness(
        tmp_path,
        {
            "bridge.launch.model": "claude-opus-4-8",
            "bridge.launch.effort": "xhigh",
            "bridge.launch.mode": "background",
        },
    )
    assert got["selectValues"]['[data-settings-launch-model]'] == "claude-opus-4-8"
    assert got["selectValues"]['[data-settings-launch-effort]'] == "xhigh"
    assert got["selectValues"]['[data-settings-launch-mode]'] == "background"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_settings_js_changing_a_select_persists_and_reapplies(tmp_path):
    got = _run_settings_harness(
        tmp_path, {},
        interactions=(
            'const el = selects["[data-settings-theme]"];'
            'el.value = "dark";'
            'el.listeners.forEach((fn) => fn());'
        ),
    )
    assert ["bridge.appearance", "dark"] in got["setCalls"]
    assert got["themeAttr"] == "dark"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_settings_js_never_queries_the_live_launch_permission_control(tmp_path):
    """Regression seam for Task 5.3: no code path in settings.js may reach for
    `data-launch-perm` (or any other `data-launch-*` selector) -- the safe
    launch defaults it stores are for THIS page's own selects only, and must
    never be able to arm the live launch band's permission control."""
    got = _run_settings_harness(tmp_path, {"bridge.launch.mode": "background"})
    assert not any("data-launch" in sel for sel in got["queried"])


# --- Task 5.3: permission mode is never persisted or pre-armed -------------
#
# The test above proves settings.js never QUERIES a `data-launch-*` selector
# in isolation. This harness goes one step further: it runs settings.js AND
# launch.js in the SAME process against the SAME seeded localStorage -- safe
# model/effort defaults plus a terminal/background launch mode (NOT a
# permission value; permission is never a stored default) -- and proves the
# storage path is real end to end: settings.js restores the defaults into the
# Settings page's own selects, and launch.js's prefill carries model/effort to
# the live launch band and the mode to the schedule form. The one thing it
# must never do is touch the permission control: `[data-launch-perm]` and the
# request `bridgeLaunchBody` builds from it both stay on the no-flag default
# regardless of what is in storage.
PERMISSION_NEVER_PERSISTED_HARNESS = """
globalThis.window = globalThis;

globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });

// Safe launch defaults for model + effort, plus a terminal/background launch
// mode -- exactly the shape the Settings page now stores. NONE of these is a
// permission value.
const store = {
  "bridge.launch.model": "claude-opus-4-8",
  "bridge.launch.effort": "xhigh",
  "bridge.launch.mode": "background",
};
globalThis.localStorage = {
  getItem(key) { return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
  setItem(key, value) { store[key] = value; },
  removeItem(key) { delete store[key]; },
};

const documentElement = {
  attrs: {},
  setAttribute(name, value) { this.attrs[name] = value; },
  removeAttribute(name) { delete this.attrs[name]; },
  getAttribute(name) { return this.attrs[name] ?? null; },
};

function makeControl(initialValue) {
  return { value: initialValue, addEventListener() {} };
}

// A `<select>`-shaped stub that carries the options it renders, so launch.js's
// "apply only a value that matches an existing option" guard has something to
// match against -- exactly how a real launch band's selects behave.
function makeSelect(initialValue, options) {
  return { value: initialValue, options: options.map((value) => ({ value })) };
}

// The Settings page's OWN "Safe launch defaults" selects -- a different
// hook (`data-settings-launch-*`) than the live launch band's.
const settingsModel = makeControl("opus");
const settingsEffort = makeControl("low");
const settingsMode = makeControl("terminal");

// The LIVE launch band's controls. The permission select is rendered at
// exactly what `_launch.html` always emits: the no-flag option (`loop.first`),
// never keyed off any suggestion or stored value.
const launchModel = makeSelect("claude-sonnet-4-6", ["claude-sonnet-4-6", "claude-opus-4-8"]);
const launchEffort = makeSelect("medium", ["low", "medium", "high", "xhigh"]);
const scheduleMode = makeSelect("terminal", ["terminal", "background"]);
const launchPerm = makeSelect("", ["", "acceptEdits", "bypassPermissions"]);
const launchBand = {
  getAttribute(name) { return name === "data-launch-path" ? "/Users/mitsheth/dev/demo" : null; },
};

// settings.js reaches these by querySelector; launch.js's prefill reaches the
// live band's selects by querySelectorAll (one entry each here).
const singular = {
  "[data-settings-launch-model]": settingsModel,
  "[data-settings-launch-effort]": settingsEffort,
  "[data-settings-launch-mode]": settingsMode,
  '[data-launch-model="launch-1"]': launchModel,
  '[data-launch-effort="launch-1"]': launchEffort,
  '[data-launch-perm="launch-1"]': launchPerm,
  '[data-launch="launch-1"]': launchBand,
};
const plural = {
  "[data-launch-model]": [launchModel],
  "[data-launch-effort]": [launchEffort],
  "[data-schedule-mode]": [scheduleMode],
};

globalThis.document = {
  documentElement,
  addEventListener() {},
  getElementById: () => null,
  querySelector(sel) { return singular[sel] ?? null; },
  querySelectorAll(sel) { return plural[sel] ?? []; },
  createRange: () => ({}),
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));  // settings.js
eval(fs.readFileSync(process.argv[3], "utf8"));  // launch.js

const body = window.bridgeLaunchBody("launch-1", "/Users/mitsheth/dev/demo");

console.log(JSON.stringify({
  settingsModelValue: settingsModel.value,
  settingsEffortValue: settingsEffort.value,
  settingsModeValue: settingsMode.value,
  launchModelValue: launchModel.value,
  launchEffortValue: launchEffort.value,
  scheduleModeValue: scheduleMode.value,
  launchPermValue: launchPerm.value,
  permissionModeSent: body.permission_mode,
}));
"""


def _run_permission_never_persisted_harness(tmp_path) -> dict:
    harness = tmp_path / "permission_never_persisted_harness.js"
    harness.write_text(PERMISSION_NEVER_PERSISTED_HARNESS)
    proc = subprocess.run(
        [_node(), str(harness), str(SETTINGS_JS), str(LAUNCH_JS)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_stored_defaults_reach_settings_but_never_arm_the_live_permission_control(tmp_path):
    """Regression proof for Task 5.3: seeds localStorage with safe model/effort
    defaults plus a terminal/background launch mode, then runs settings.js and
    launch.js against the same document. The storage path is real end to end --
    it reaches the Settings page's own model/effort/mode selects AND launch.js
    prefills the live launch band's model/effort selects and the schedule
    form's mode -- which is what makes the exclusion below meaningful rather
    than "nothing reads storage at all". But `bridgeLaunchBody` still sends the
    no-flag permission mode, and the live launch band's permission select's
    selected option is still the no-flag one: permission is never a stored
    default and is never pre-armed."""
    got = _run_permission_never_persisted_harness(tmp_path)

    # The storage path is real on both sides. Settings' own selects:
    assert got["settingsModelValue"] == "claude-opus-4-8"
    assert got["settingsEffortValue"] == "xhigh"
    assert got["settingsModeValue"] == "background"
    # And launch.js's prefill actually reaches the live launch band + schedule
    # form -- so the exclusion below is a real carve-out, not a dead path.
    assert got["launchModelValue"] == "claude-opus-4-8"
    assert got["launchEffortValue"] == "xhigh"
    assert got["scheduleModeValue"] == "background"

    # But the live launch band's permission control is never pre-armed by it.
    assert got["launchPermValue"] == "", (
        "a stored value leaked into the live launch band's permission select"
    )
    assert got["permissionModeSent"] == "", (
        "bridgeLaunchBody sent a permission mode that came from storage "
        "instead of the no-flag default the launch band always renders"
    )


# --- Task 5.3 (cont.): launch.js prefill in isolation -----------------------
#
# The proof above runs settings.js + launch.js together. This one drives ONLY
# launch.js's prefill, so it pins that module's own contract: a stored default
# is applied to a live select only when it matches an existing option (an
# unknown default is skipped, not forced), and the permission control is never
# touched.
LAUNCH_PREFILL_HARNESS = """
globalThis.window = globalThis;

const store = {
  "bridge.launch.model": "claude-opus-4-8",
  "bridge.launch.effort": "xhigh",
  "bridge.launch.mode": "background",
};
globalThis.localStorage = {
  getItem(key) { return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
  setItem() {}, removeItem() {},
};

// Each select can resolve its `[data-launch]` band via `closest`, exactly as a
// real select nested inside the band does. `band` is null for a schedule-mode
// select (no enclosing launch band), a handoff-free band, or a band that
// carries a handoff suggestion.
function makeBand(handoffId) {
  return { getAttribute: (name) => (name === "data-launch-handoff" ? handoffId : null) };
}
function makeSelect(initialValue, options, band) {
  return {
    value: initialValue,
    options: options.map((value) => ({ value })),
    closest: (sel) => (sel === "[data-launch]" ? (band ?? null) : null),
  };
}

const bandPlain = makeBand(null);
const bandHandoff = makeBand("h1");

// A handoff-free band takes the stored default.
const model = makeSelect("claude-sonnet-4-6", ["claude-sonnet-4-6", "claude-opus-4-8"], bandPlain);
const effort = makeSelect("medium", ["low", "medium", "high", "xhigh"], bandPlain);
// A band carrying a handoff SUGGESTION must keep its server-selected value --
// the contextual suggestion wins over the generic browser-wide default.
const modelHandoff = makeSelect("claude-sonnet-4-6", ["claude-sonnet-4-6", "claude-opus-4-8"], bandHandoff);
const effortHandoff = makeSelect("low", ["low", "medium", "high", "xhigh"], bandHandoff);
// A handoff-free band whose catalog does NOT include the stored default: the
// "valid option only" guard must leave it untouched.
const modelNoMatch = makeSelect("claude-haiku", ["claude-haiku"], bandPlain);
const scheduleMode = makeSelect("terminal", ["terminal", "background"], null);
const perm = makeSelect("", ["", "acceptEdits", "bypassPermissions"], bandPlain);

const plural = {
  "[data-launch-model]": [model, modelHandoff, modelNoMatch],
  "[data-launch-effort]": [effort, effortHandoff],
  "[data-schedule-mode]": [scheduleMode],
};
const singular = {
  '[data-launch-model="launch-1"]': model,
  '[data-launch-effort="launch-1"]': effort,
  '[data-launch-perm="launch-1"]': perm,
  '[data-launch="launch-1"]': {
    getAttribute: (name) => (name === "data-launch-path" ? "/Users/mitsheth/dev/demo" : null),
  },
};

globalThis.document = {
  addEventListener() {},
  getElementById: () => null,
  querySelector(sel) { return singular[sel] ?? null; },
  querySelectorAll(sel) { return plural[sel] ?? []; },
  createRange: () => ({}),
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));  // launch.js

const body = window.bridgeLaunchBody("launch-1", "/Users/mitsheth/dev/demo");

console.log(JSON.stringify({
  modelValue: model.value,
  modelHandoffValue: modelHandoff.value,
  modelNoMatchValue: modelNoMatch.value,
  effortValue: effort.value,
  effortHandoffValue: effortHandoff.value,
  scheduleModeValue: scheduleMode.value,
  permValue: perm.value,
  permissionModeSent: body.permission_mode,
}));
"""


def _run_launch_prefill_harness(tmp_path) -> dict:
    harness = tmp_path / "launch_prefill_harness.js"
    harness.write_text(LAUNCH_PREFILL_HARNESS)
    proc = subprocess.run(
        [_node(), str(harness), str(LAUNCH_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_launch_js_prefills_the_band_from_stored_defaults_but_never_the_permission(tmp_path):
    got = _run_launch_prefill_harness(tmp_path)
    # A handoff-free band with a matching option takes the stored default.
    assert got["modelValue"] == "claude-opus-4-8"
    assert got["effortValue"] == "xhigh"
    assert got["scheduleModeValue"] == "background"
    # A band carrying a handoff suggestion keeps its server-selected value even
    # though the same stored defaults are a valid option -- the suggestion wins.
    assert got["modelHandoffValue"] == "claude-sonnet-4-6"
    assert got["effortHandoffValue"] == "low"
    # A stored default with no matching option is skipped, not forced on.
    assert got["modelNoMatchValue"] == "claude-haiku"
    # The permission control is never touched, and the request it feeds stays
    # on the no-flag default.
    assert got["permValue"] == ""
    assert got["permissionModeSent"] == ""
