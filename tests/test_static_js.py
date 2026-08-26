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
from datetime import datetime
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
      name === "data-launch-path" ? "/Users/you/dev/demo" : null,
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


# --- Task 5 fix round 1: stacked launch bands fire independently -------------
#
# `_launch.html` now renders one `launch_band` per queued handoff, each with
# its own `lid` (`launch-<handoff-id>`, keyed off the handoff since the fix).
# This harness puts TWO such bands' worth of controls in the DOM stub at once
# -- `launch-h1` and `launch-h2` -- with deliberately DIFFERENT model/effort/
# permission values, and clicks the SECOND band's button. Every selector
# `launch.js` resolves is an exact match on the id read off the clicked
# element, so this is the only way to prove a click on one stacked band can
# never read another's selects (the bug `lid` colliding on `card.project_id`
# used to cause: whichever band happened to match first would answer, however
# many handoffs were queued).
STACKED_HARNESS = """
globalThis.window = globalThis;
let clickHandlers = [];
const controls = {
  '[data-launch-model="launch-h1"]': { value: "claude-opus-4-8" },
  '[data-launch-effort="launch-h1"]': { value: "low" },
  '[data-launch-perm="launch-h1"]': { value: "" },
  '[data-launch="launch-h1"]': {
    getAttribute: (name) => ({
      "data-launch-path": "/Users/you/dev/demo",
      "data-launch-handoff": "h1",
      "data-launch-prompt": "handoff-h1",
    }[name] ?? null),
  },
  '[data-launch-model="launch-h2"]': { value: "claude-sonnet-5" },
  '[data-launch-effort="launch-h2"]': { value: "xhigh" },
  '[data-launch-perm="launch-h2"]': { value: "bypassPermissions" },
  '[data-launch="launch-h2"]': {
    getAttribute: (name) => ({
      "data-launch-path": "/Users/you/dev/demo",
      "data-launch-handoff": "h2",
      "data-launch-prompt": "handoff-h2",
    }[name] ?? null),
  },
};
const fields = {
  "handoff-h1": { value: "prompt for h1" },
  "handoff-h2": { value: "prompt for h2" },
};
globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandlers.push(fn); },
  getElementById: (id) => fields[id] ?? null,
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

// Only the SECOND band's button is clicked -- the first band's controls are
// present in `controls` above purely as a decoy the click must not read.
const button = {
  getAttribute: () => "launch-h2",
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


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_launch_button_click_reads_only_its_own_stacked_band(tmp_path):
    harness = tmp_path / "stacked_harness.js"
    harness.write_text(STACKED_HARNESS)
    proc = subprocess.run(
        [_node(), str(harness), str(LAUNCH_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)

    # The second band's own values -- never the first band's.
    assert body["model"] == "claude-sonnet-5"
    assert body["effort"] == "xhigh"
    assert body["permission_mode"] == "bypassPermissions"
    assert body["handoff_id"] == "h2"
    assert body["prompt"] == "prompt for h2"


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
  attrs: { "data-launch": "launch-1", "data-launch-path": "/Users/you/dev/demo",
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


# --- Final-review fix round: the "Dismiss handoff" click handler -----------
#
# `launch.js` registers a SECOND delegated "click" listener for
# `[data-handoff-dismiss]` (Task 3.3). This harness mirrors `LAUNCH_HARNESS`'s
# shape -- a stub DOM keyed by selector, a captured `fetch` call, the real
# file evaluated under `node` -- but drives that second listener instead of
# the launch-button one, and inspects the DOM nodes the handler is supposed to
# patch directly (never `innerHTML`, never a reload).
#
# Two handoffs (h1, h2) are staged, dismissed one at a time in the same
# harness run, and BOTH intermediate and final DOM state are captured. That
# is what the previous version of this harness got wrong: it fabricated a
# lone handoff whose id ("1") happened to equal the compose box's project-id
# suffix ("compose-1") -- an alignment the real template never produces
# (handoff ids are opaque strings; the compose id is `compose-<project_id>`,
# always rendered regardless of which handoffs are queued). That let a
# now-deleted "demote the band to the compose id" code path look correct by
# coincidence. `compose-42` here is a project id that matches NEITHER handoff
# id, closing that gap; the dismiss handler no longer touches it at all,
# which this harness proves by never exercising `getElementById` from the
# handler's own code path.
DISMISS_HARNESS = """
globalThis.window = globalThis;
let clickHandlers = [];

function section() { return { hidden: false }; }
function band(id) {
  return {
    attrs: { "data-launch-handoff": id, "data-launch-prompt": `handoff-${id}`, "data-launch": `launch-${id}` },
    hidden: false,
  };
}
function dismissButton(id) {
  const self = { getAttribute: () => id, disabled: false, hidden: false, closest: (sel) => (sel === "[data-handoff-dismiss]" ? self : null) };
  return self;
}
function status() { return { textContent: "" }; }

const composeField = { value: "" };
const sectionH1 = section();
const sectionH2 = section();
const bandH1 = band("h1");
const bandH2 = band("h2");
const buttonH1 = dismissButton("h1");
const buttonH2 = dismissButton("h2");
const statusH1 = status();
const statusH2 = status();
const empty = { hidden: true };

const nodes = {
  '[data-handoff-section="h1"]': sectionH1,
  '[data-handoff-section="h2"]': sectionH2,
  '[data-launch-handoff="h1"]': bandH1,
  '[data-launch-handoff="h2"]': bandH2,
  '[data-handoff-dismiss-status="h1"]': statusH1,
  '[data-handoff-dismiss-status="h2"]': statusH2,
  "[data-handoff-empty]": empty,
};
const sections = { h1: sectionH1, h2: sectionH2 };

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandlers.push(fn); },
  querySelector: (sel) => nodes[sel] ?? null,
  querySelectorAll(sel) {
    if (sel === "[data-handoff-section]") return Object.values(sections);
    return [];
  },
  getElementById: (id) => (id === "compose-42" ? composeField : null),
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

function fire(button) {
  return Promise.all(clickHandlers.map((fn) => fn({ target: button })));
}
function snapshot() {
  return {
    fetchCalls: fetchCalls.slice(),
    sectionH1Hidden: sectionH1.hidden,
    sectionH2Hidden: sectionH2.hidden,
    bandH1Hidden: bandH1.hidden,
    bandH2Hidden: bandH2.hidden,
    buttonH1Hidden: buttonH1.hidden,
    buttonH2Hidden: buttonH2.hidden,
    statusH1: statusH1.textContent,
    statusH2: statusH2.textContent,
    emptyHidden: empty.hidden,
    reloadCalled,
    hrefAssigned,
  };
}

fire(buttonH1).then(() => {
  const afterH1 = snapshot();
  return fire(buttonH2).then(() => {
    console.log(JSON.stringify({ afterH1, afterH2: snapshot() }));
  });
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
    after_h1 = result["afterH1"]
    assert len(after_h1["fetchCalls"]) == 1
    call = after_h1["fetchCalls"][0]
    assert call["url"] == "/api/handoff/h1"
    assert call["method"] == "PATCH"
    assert json.loads(call["body"]) == {"status": "dismissed"}


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dismiss_handoff_updates_the_dom_in_place_on_success(tmp_path):
    result = _run_dismiss_harness(tmp_path)
    after_h1 = result["afterH1"]
    # The handoff section, its launch band, and its dismiss button all hide
    # in place -- flipping `hidden` on already-rendered nodes, never
    # rebuilding markup and never leaving a mis-targeted control behind (the
    # obsolete "demote to the compose id" path is gone entirely: there is no
    # band left keyed off `handoff-h1` for a stray click to reach).
    assert after_h1["sectionH1Hidden"] is True
    assert after_h1["bandH1Hidden"] is True
    assert after_h1["buttonH1Hidden"] is True
    # A role=status announcement, not a reload.
    assert "Dismissed" in after_h1["statusH1"]
    assert after_h1["reloadCalled"] is False
    assert after_h1["hrefAssigned"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dismiss_handoff_status_span_stays_live_while_its_button_hides(tmp_path):
    # The status span is a SIBLING of the dismiss button, not a descendant --
    # hiding the button must never take the span down with it, since
    # `announce(key, "✓ Dismissed")` only reaches something still in the
    # accessibility tree.
    result = _run_dismiss_harness(tmp_path)
    after_h1 = result["afterH1"]
    assert after_h1["buttonH1Hidden"] is True
    assert after_h1["statusH1"] == "✓ Dismissed"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dismissing_one_handoff_leaves_its_sibling_untouched_and_the_empty_state_hidden(
    tmp_path,
):
    # This is the case that catches the regression the review found: the old
    # handler unhid the empty-state UNCONDITIONALLY on every dismiss. With a
    # sibling handoff still queued, "no session in progress" must stay a lie
    # the page never tells.
    result = _run_dismiss_harness(tmp_path)
    after_h1 = result["afterH1"]
    assert after_h1["sectionH2Hidden"] is False
    assert after_h1["bandH2Hidden"] is False
    assert after_h1["buttonH2Hidden"] is False
    assert after_h1["statusH2"] == ""
    assert after_h1["emptyHidden"] is True


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dismissing_every_queued_handoff_reveals_the_empty_state(tmp_path):
    result = _run_dismiss_harness(tmp_path)
    after_h2 = result["afterH2"]
    assert after_h2["sectionH1Hidden"] is True
    assert after_h2["sectionH2Hidden"] is True
    assert after_h2["emptyHidden"] is False


# --- Task 6 fix round 1: dismiss binds per stacked handoff too -------------
#
# The `DISMISS_HARNESS` above proves the dismiss handler's *shape* (PATCH,
# DOM patched in place, band + button hidden, empty-state gated on remaining
# sections) by dismissing two stacked handoffs in sequence. It says nothing
# about SELECTOR precision when two handoffs are BOTH still on the page: the
# handler resolves `id` off the CLICKED button
# (`event.target.closest("[data-handoff-dismiss]")`, then
# `button.getAttribute("data-handoff-dismiss")`), then reaches every other
# node through that same id (`[data-handoff-section="${id}"]`,
# `[data-launch-handoff="${id}"]`, `[data-handoff-dismiss-status="${id}"]`).
# This harness stacks two full sets of those nodes (h1, h2) and fires dismiss
# on only one -- proving the PATCH and every DOM patch land on that handoff
# alone and the sibling's section/band/status are untouched.
#
# `dismissDecoy` is the trap: the real handler never calls a bare
# `document.querySelector("[data-handoff-dismiss]")` (the button comes from
# `event.target.closest`, not a document-level lookup), so this selector is
# never hit by correct code. If the handler regressed to that first-match
# form, `button` would resolve here to a hard-coded "h1" -- silently
# answering for a click actually aimed at h2 -- and every assertion below
# would fail. The same trap is set on the bare (id-less) section/band/status
# selectors: dropping the id interpolation on any one of them would resolve
# to h1's node instead of returning null, which the assertions also catch.
STACKED_DISMISS_HARNESS = """
globalThis.window = globalThis;
let clickHandlers = [];

function section(hidden) { return { hidden }; }
function band(id, launchId) {
  return {
    attrs: {
      "data-launch-handoff": id,
      "data-launch-prompt": `handoff-${id}`,
      "data-launch": launchId,
    },
    hidden: false,
  };
}
function status() { return { textContent: "" }; }

const sectionH1 = section(false);
const sectionH2 = section(false);
const bandH1 = band("h1", "launch-h1");
const bandH2 = band("h2", "launch-h2");
const statusH1 = status();
const statusH2 = status();
const empty = { hidden: true };
const composeField = { value: "" };

// Decoys: a correct handler never reaches these bare (id-less) selectors.
const dismissDecoy = { getAttribute: () => "h1" };

const nodes = {
  '[data-handoff-section="h1"]': sectionH1,
  '[data-handoff-section="h2"]': sectionH2,
  '[data-launch-handoff="h1"]': bandH1,
  '[data-launch-handoff="h2"]': bandH2,
  '[data-handoff-dismiss-status="h1"]': statusH1,
  '[data-handoff-dismiss-status="h2"]': statusH2,
  "[data-handoff-empty]": empty,
  "[data-handoff-dismiss]": dismissDecoy,
  "[data-handoff-section]": sectionH1,
  "[data-launch-handoff]": bandH1,
  "[data-handoff-dismiss-status]": statusH1,
};
const sections = { h1: sectionH1, h2: sectionH2 };

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandlers.push(fn); },
  querySelector: (sel) => nodes[sel] ?? null,
  querySelectorAll(sel) {
    if (sel === "[data-handoff-section]") return Object.values(sections);
    return [];
  },
  getElementById: (id) => (id === "compose-42" ? composeField : null),
};

const fetchCalls = [];
globalThis.fetch = async (url, init) => {
  fetchCalls.push({ url, method: init.method, body: init.body });
  return { ok: true, status: 200, json: async () => ({}) };
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

// The button that ACTUALLY fired -- its own `closest` resolves to itself,
// mirroring a real click event's `.target.closest(...)`.
function dismissButton(id) {
  const self = {
    getAttribute: () => id,
    disabled: false,
    hidden: false,
    closest: (sel) => (sel === "[data-handoff-dismiss]" ? self : null),
  };
  return self;
}
const buttonH1 = dismissButton("h1");
const buttonH2 = dismissButton("h2");

const target = TARGET === "h1" ? buttonH1 : buttonH2;
const event = { target };
Promise.all(clickHandlers.map((fn) => fn(event))).then(() => {
  console.log(JSON.stringify({
    fetchCalls,
    sectionH1Hidden: sectionH1.hidden,
    sectionH2Hidden: sectionH2.hidden,
    bandH1Hidden: bandH1.hidden,
    bandH2Hidden: bandH2.hidden,
    buttonH1Hidden: buttonH1.hidden,
    buttonH2Hidden: buttonH2.hidden,
    statusH1: statusH1.textContent,
    statusH2: statusH2.textContent,
    emptyHidden: empty.hidden,
  }));
});
"""


def _run_stacked_dismiss(tmp_path, target: str) -> dict:
    harness = tmp_path / f"stacked_dismiss_{target}.js"
    harness.write_text(STACKED_DISMISS_HARNESS.replace("TARGET", json.dumps(target)))
    proc = subprocess.run(
        [_node(), str(harness), str(LAUNCH_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dismissing_the_second_stacked_handoff_never_touches_the_first(tmp_path):
    got = _run_stacked_dismiss(tmp_path, "h2")
    assert len(got["fetchCalls"]) == 1
    call = got["fetchCalls"][0]
    assert call["url"] == "/api/handoff/h2"
    assert call["method"] == "PATCH"
    assert json.loads(call["body"]) == {"status": "dismissed"}
    assert got["sectionH2Hidden"] is True
    assert got["bandH2Hidden"] is True
    assert got["buttonH2Hidden"] is True
    assert "Dismissed" in got["statusH2"]
    # The sibling handoff's section, band, button, and status are untouched.
    assert got["sectionH1Hidden"] is False
    assert got["bandH1Hidden"] is False
    assert got["buttonH1Hidden"] is False
    assert got["statusH1"] == ""
    # A sibling handoff is still queued, so the empty-state must stay hidden.
    assert got["emptyHidden"] is True


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dismissing_the_first_stacked_handoff_never_touches_the_second(tmp_path):
    """Firing on the FIRST stacked dismiss button must resolve to h1, not
    fall through to h2 -- proving the id comes from the clicked button
    itself, not from DOM order or a single first-match lookup that would
    always answer with whichever handoff is first."""
    got = _run_stacked_dismiss(tmp_path, "h1")
    assert len(got["fetchCalls"]) == 1
    call = got["fetchCalls"][0]
    assert call["url"] == "/api/handoff/h1"
    assert json.loads(call["body"]) == {"status": "dismissed"}
    assert got["sectionH1Hidden"] is True
    assert got["bandH1Hidden"] is True
    assert got["buttonH1Hidden"] is True
    assert "Dismissed" in got["statusH1"]
    assert got["sectionH2Hidden"] is False
    assert got["bandH2Hidden"] is False
    assert got["buttonH2Hidden"] is False
    assert got["statusH2"] == ""
    assert got["emptyHidden"] is True


# --- Task 6: prompt-save (focusout) binds per element, not a shared lookup --
#
# `launch.js`'s focusout listener resolves the handoff id off the FIELD THAT
# FIRED -- `event.target.closest("[data-prompt-handoff]")`, then
# `field.getAttribute("data-prompt-handoff")` -- rather than a single
# `document.querySelector('[data-prompt-handoff]')` that would always answer
# with whichever prompt textarea happens to be first in the DOM. Two queued
# handoffs on one project (Task 5) each render their own prompt textarea; this
# harness puts BOTH in the DOM stub at once, each with its own edited value,
# and fires focusout on only ONE of them per run -- proving a save on either
# field reaches its OWN handoff's PATCH and its OWN status span, never the
# other stacked field's.
PROMPT_SAVE_HARNESS = """
globalThis.window = globalThis;
let focusoutHandlers = [];

function field(id, handoffId, value, saved) {
  const self = {
    id, value, defaultValue: saved, dataset: {},
    getAttribute(name) { return name === "data-prompt-handoff" ? handoffId : null; },
    closest(sel) { return sel === "[data-prompt-handoff]" ? self : null; },
  };
  return self;
}

const fieldH1 = field("handoff-h1", "h1", "EDITED prompt for h1", "original prompt for h1");
const fieldH2 = field("handoff-h2", "h2", "EDITED prompt for h2", "original prompt for h2");

const statusNodes = {
  '[data-prompt-status="handoff-h1"]': { textContent: "" },
  '[data-prompt-status="handoff-h2"]': { textContent: "" },
};

globalThis.document = {
  addEventListener(type, fn) { if (type === "focusout") focusoutHandlers.push(fn); },
  querySelector: (sel) => statusNodes[sel] ?? null,
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };

const fetchCalls = [];
globalThis.fetch = async (url, init) => {
  fetchCalls.push({ url, method: init.method, body: JSON.parse(init.body) });
  return { ok: true, status: 200, json: async () => ({}) };
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

// Only ONE stacked field's focusout fires per run -- the other sits in the
// DOM stub purely as a decoy the handler must not read from or write to.
const target = FIELD === "h1" ? fieldH1 : fieldH2;
const event = { target };
Promise.all(focusoutHandlers.map((fn) => fn(event))).then(() => {
  console.log(JSON.stringify({
    fetchCalls,
    statusH1: statusNodes['[data-prompt-status="handoff-h1"]'].textContent,
    statusH2: statusNodes['[data-prompt-status="handoff-h2"]'].textContent,
  }));
});
"""


def _run_prompt_save(tmp_path, field_name: str) -> dict:
    harness = tmp_path / f"prompt_save_{field_name}.js"
    harness.write_text(PROMPT_SAVE_HARNESS.replace("FIELD", json.dumps(field_name)))
    proc = subprocess.run(
        [_node(), str(harness), str(LAUNCH_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_prompt_save_on_the_second_stacked_field_never_touches_the_first(tmp_path):
    got = _run_prompt_save(tmp_path, "h2")
    assert len(got["fetchCalls"]) == 1
    call = got["fetchCalls"][0]
    assert call["url"] == "/api/handoff/h2"
    assert call["method"] == "PATCH"
    assert call["body"] == {"next_prompt": "EDITED prompt for h2"}
    assert "saved" in got["statusH2"]
    # The other stacked handoff's status span is untouched.
    assert got["statusH1"] == ""


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_prompt_save_on_the_first_stacked_field_never_touches_the_second(tmp_path):
    """Firing on the FIRST stacked field must resolve to h1, not fall through
    to h2 -- proving the id comes from the fired element itself, not from DOM
    order or a single first-match `querySelector` that would always answer
    with whichever prompt field is first."""
    got = _run_prompt_save(tmp_path, "h1")
    assert len(got["fetchCalls"]) == 1
    call = got["fetchCalls"][0]
    assert call["url"] == "/api/handoff/h1"
    assert call["body"] == {"next_prompt": "EDITED prompt for h1"}
    assert "saved" in got["statusH1"]
    assert got["statusH2"] == ""


# --- Codex review finding #2: out-of-order prompt-save completion -----------
#
# `savePrompt` used to read `field.value` (the CURRENT value) at completion
# time to decide what got saved, rather than the value it actually sent. Two
# distinct corruptions follow from that:
#
#   1. A single save can be in flight while the user keeps typing (no second
#      save has fired yet). When it resolves, the old code stamps
#      `dataset.savedPrompt` with whatever is in the field NOW, not what the
#      server actually received -- so a later blur/leave sees
#      `field.value === savedPrompt` and skips the PATCH entirely, silently
#      losing the newer text forever.
#   2. Two saves can be in flight at once (a blur save and an `onLeave`
#      flush overlap easily). If the OLDER one resolves LAST, the old code
#      re-stamps `savedPrompt` with whatever is in the field at that moment,
#      overwriting the correct value the NEWER save already recorded.
#
# The fix captures the submitted value before the `await` and only lets a
# save touch `savedPrompt` if it is still the newest save issued for that
# field. The harness gives each `fetch` call a controllable, out-of-order
# resolution instead of resolving inline, so completion order is driven by
# the test, not call order.
PROMPT_RACE_HARNESS = """
globalThis.window = globalThis;
let focusoutHandlers = [];
let enterHooks = [];
let leaveHooks = [];

function field(id, handoffId, value, saved) {
  const self = {
    id, value, defaultValue: saved, dataset: {},
    getAttribute(name) { return name === "data-prompt-handoff" ? handoffId : null; },
    closest(sel) { return sel === "[data-prompt-handoff]" ? self : null; },
  };
  return self;
}

const promptField = field("handoff-h1", "h1", "A", "orig");
const statusNode = { textContent: "" };

globalThis.document = {
  addEventListener(type, fn) { if (type === "focusout") focusoutHandlers.push(fn); },
  querySelector: (sel) => (sel === '[data-prompt-status="handoff-h1"]' ? statusNode : null),
  querySelectorAll: (sel) => (sel === "[data-prompt-handoff]" ? [promptField] : []),
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };

// Every fetch call is captured but never resolved inline -- the test script
// resolves them by index, in whatever order it chooses.
const pending = [];
globalThis.fetch = (url, init) => new Promise((resolve, reject) => {
  pending.push({ url, body: JSON.parse(init.body), resolve, reject });
});

const storageMap = new Map();
globalThis.sessionStorage = {
  getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
};

window.bridgePage = {
  onEnter(fn) { enterHooks.push(fn); },
  onLeave(fn) { leaveHooks.push(fn); },
  onMorph() {},
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

function fireSave() {
  focusoutHandlers.forEach((fn) => fn({ target: promptField }));
}
function resolveOk(index) {
  pending[index].resolve({ ok: true, status: 200, json: async () => ({}) });
}
function tick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

(async () => {
  SCRIPT
  console.log(JSON.stringify({
    pendingBodies: pending.map((p) => p.body),
    savedPrompt: promptField.dataset.savedPrompt ?? null,
    fieldValue: promptField.value,
    status: statusNode.textContent,
    storage: Object.fromEntries(storageMap),
  }));
})();
"""


def _run_prompt_race(tmp_path, name: str, script: str) -> dict:
    harness = tmp_path / f"prompt_race_{name}.js"
    harness.write_text(PROMPT_RACE_HARNESS.replace("SCRIPT", script))
    proc = subprocess.run(
        [_node(), str(harness), str(LAUNCH_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_resolving_save_records_what_it_sent_not_the_current_field_value(tmp_path):
    """One save in flight for "A"; the user keeps typing to "C" with no second
    save fired. On success, `savedPrompt` must be "A" (what the server got),
    never "C" -- stamping "C" would make a later leave believe "C" is already
    saved and skip the PATCH that would actually persist it."""
    got = _run_prompt_race(tmp_path, "single", """
    fireSave();
    promptField.value = "C";
    resolveOk(0);
    await tick();
    """)
    assert got["pendingBodies"] == [{"next_prompt": "A"}]
    assert got["savedPrompt"] == "A"
    assert got["fieldValue"] == "C"
    assert "saved" in got["status"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_an_older_save_resolving_last_does_not_clobber_a_newer_ones_result(tmp_path):
    """Two saves overlap: the first submits "A", then the field changes to "B"
    and a second save submits "B", then the field changes again to "D" with no
    third save fired. The NEWER save ("B") resolves first; the OLDER save
    ("A") resolves last. `savedPrompt` must end at "B" -- the newest
    acknowledged submission -- never "A" (the late, stale response) and never
    "D" (never actually sent)."""
    got = _run_prompt_race(tmp_path, "double", """
    fireSave();
    promptField.value = "B";
    fireSave();
    promptField.value = "D";
    resolveOk(1);
    await tick();
    resolveOk(0);
    await tick();
    """)
    assert got["pendingBodies"] == [{"next_prompt": "A"}, {"next_prompt": "B"}]
    assert got["savedPrompt"] == "B"


# --- Codex review finding #1: a failed autosave must not lose the edit ------
#
# A failed `savePrompt` used to only announce a warning and resolve normally --
# `onLeave` awaited it, saw a resolved (not rejected) promise, and the router
# swapped the editor away with nothing keeping the text anywhere. The fix
# mirrors the existing compose-draft mechanism: a failed save is persisted to
# `sessionStorage` under `bridge.handoff-draft.<handoffId>`, a successful save
# clears it, and `bridgePage.onEnter` restores it into the freshly re-rendered
# field (which the server always pre-fills with its own last-saved value, so a
# stale draft would otherwise be silently overwritten by that server value).
PROMPT_DRAFT_HARNESS = """
globalThis.window = globalThis;
let focusoutHandlers = [];
let enterHooks = [];

function field(id, handoffId, value, saved) {
  const self = {
    id, value, defaultValue: saved, dataset: {},
    getAttribute(name) { return name === "data-prompt-handoff" ? handoffId : null; },
    closest(sel) { return sel === "[data-prompt-handoff]" ? self : null; },
  };
  return self;
}

const promptField = field("handoff-h1", "h1", VALUE, SAVED);
const statusNode = { textContent: "" };

globalThis.document = {
  addEventListener(type, fn) { if (type === "focusout") focusoutHandlers.push(fn); },
  querySelector: (sel) => (sel === '[data-prompt-status="handoff-h1"]' ? statusNode : null),
  querySelectorAll: (sel) => (sel === "[data-prompt-handoff]" ? [promptField] : []),
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };

globalThis.fetch = async () => (FETCH_OK
  ? { ok: true, status: 200, json: async () => ({}) }
  : Promise.reject(new Error("network down")));

const storageMap = new Map();
for (const [k, v] of Object.entries(PRELOAD)) storageMap.set(k, v);
globalThis.sessionStorage = {
  getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
};

window.bridgePage = {
  onEnter(fn) { enterHooks.push(fn); },
  onLeave() {},
  onMorph() {},
  enter() { enterHooks.forEach((fn) => fn()); },
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

// The focusout listener itself does not return `savePrompt`'s promise (a real
// DOM event handler cannot be awaited by its dispatcher either), so calling
// the handlers alone proves nothing about completion -- a `tick()` past the
// event loop is what actually lets the fetch's microtask chain finish.
function fireSave() {
  focusoutHandlers.forEach((fn) => fn({ target: promptField }));
  return tick();
}
function tick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

(async () => {
  SCRIPT
  console.log(JSON.stringify({
    fieldValue: promptField.value,
    status: statusNode.textContent,
    storage: Object.fromEntries(storageMap),
  }));
})();
"""


def _run_prompt_draft(
    tmp_path, name: str, script: str, *, value: str, saved: str,
    fetch_ok: bool = True, preload: dict | None = None,
) -> dict:
    harness = tmp_path / f"prompt_draft_{name}.js"
    text = (
        PROMPT_DRAFT_HARNESS
        .replace("SCRIPT", script)
        .replace("VALUE", json.dumps(value))
        .replace("SAVED", json.dumps(saved))
        .replace("FETCH_OK", "true" if fetch_ok else "false")
        .replace("PRELOAD", json.dumps(preload or {}))
    )
    harness.write_text(text)
    proc = subprocess.run(
        [_node(), str(harness), str(LAUNCH_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_failed_save_persists_its_text_to_session_storage(tmp_path):
    got = _run_prompt_draft(
        tmp_path, "fail", "await fireSave();",
        value="edited but unsaved", saved="orig", fetch_ok=False,
    )
    assert got["storage"] == {"bridge.handoff-draft.h1": "edited but unsaved"}


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_successful_save_clears_any_stale_draft(tmp_path):
    got = _run_prompt_draft(
        tmp_path, "success", "await fireSave();",
        value="B", saved="orig", fetch_ok=True,
        preload={"bridge.handoff-draft.h1": "a stale draft from an earlier failure"},
    )
    assert got["storage"] == {}


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_on_enter_a_freshly_rendered_field_is_restored_from_its_failed_draft(tmp_path):
    """The field arrives from the server already showing ITS last-saved value
    (value == saved, no local edit) -- exactly what a router swap re-renders
    after navigating away with a failed save still sitting in storage. `enter`
    must overwrite it with the draft and re-announce the warning, or the
    user's edit is invisible on return with no sign it was never saved."""
    got = _run_prompt_draft(
        tmp_path, "restore", "window.bridgePage.enter();",
        value="server value", saved="server value",
        preload={"bridge.handoff-draft.h1": "the text that failed to save"},
    )
    assert got["fieldValue"] == "the text that failed to save"
    assert "Not saved" in got["status"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_enter_is_a_no_op_when_the_field_already_matches_its_draft(tmp_path):
    got = _run_prompt_draft(
        tmp_path, "noop", "window.bridgePage.enter();",
        value="already matches", saved="already matches",
        preload={"bridge.handoff-draft.h1": "already matches"},
    )
    assert got["fieldValue"] == "already matches"
    assert got["status"] == ""


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
    "[data-burn-today]": node(),
    "[data-burn-last-5h]": node(), "[data-sparkline]": node(),
  };
  const bandParent = node();
  leaves["[data-live-status]"].closest = () => bandParent;
  root.querySelector = (sel) => leaves[sel] || null;
  root.textarea = { value: `typed ${id}` };
  return root;
}
const cards = [card("1"), card("2")];
const cardMap = Object.fromEntries(cards.map((item) => [item.getAttribute("data-project-card"), item]));

const selectors = {
  "[data-freshness-strip]": strip, "[data-freshness-label]": label,
  "[data-freshness-age]": age,
  "[data-diagnostics-alert]": diagnostics,
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
  // live.js reads totals with querySelectorAll (the Overview renders each one
  // twice), so this has to answer for the same selectors querySelector does or
  // the totals assertions below would pass vacuously against an empty list.
  querySelectorAll(sel) {
    return selectors[sel] ? [selectors[sel]] : [];
  },
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
window.bridgeApplyDashboardUpdate({ schema: 1, kind: "snapshot", generated_at: 148,
  generation: 3, freshness: { server: "unavailable", index_at: null, index_age_seconds: null },
  topbar: {}, diagnostics: { alert: true }, card_order: [], cards: {},
  refresh: { attempted: true, completed: false, error: "offline" }, unattributed: [] });
const unavailableState = label.textContent;
window.bridgeApplyDashboardUpdate(REFRESH_BODY);
const beforeRefresh = refreshStatus.textContent;
clickHandler({ target: { closest: (sel) => sel === "[data-dashboard-refresh]" ? refreshButton : null } });
setImmediate(() => {
  const afterSuccessfulRefresh = refreshStatus.textContent;
  // A failed reindex still comes back as an HTTP-success envelope carrying
  // whatever data was already on hand -- `response.ok` and
  // `applyDashboardUpdate` both pass it, so only `refresh.completed` tells
  // the two apart.
  REFRESH_BODY = { schema: 1, kind: "snapshot", generated_at: 999999, generation: 2,
    freshness: { server: "available", index_at: 146, index_age_seconds: 0 },
    topbar: {}, diagnostics: { alert: false }, card_order: [], cards: {},
    refresh: { attempted: true, completed: false, error: "index is locked" },
    unattributed: [] };
  clickHandler({ target: { closest: (sel) => sel === "[data-dashboard-refresh]" ? refreshButton : null } });
  setImmediate(() => console.log(JSON.stringify({
    stale, fresh: label.textContent, unavailableState,
    announcements,
    textareaSame: textareaIdentity.every((item, index) => item === cards[index].textarea),
    textareaValues: textareaIdentity.map((item) => item.value),
    refresh: afterSuccessfulRefresh, totals: totals.today.textContent,
    lastIndex: totals.last_index.textContent,
    stripState: strip.getAttribute("data-freshness-state"),
    beforeRefresh,
    afterFailedRefresh: refreshStatus.textContent,
  })));
});
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
    assert got["stale"] == "Stale"
    assert got["fresh"] == "Connected"
    assert got["unavailableState"] == "Unavailable"
    assert got["totals"] == "1k"
    # The visible word is sentence case; the attribute CSS and JS select on
    # stays lowercase. Both are asserted here so a future "tidy" of either one
    # cannot quietly re-split them.
    assert got["stripState"] == "connected"


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
def test_liveness_patch_does_not_reset_index_freshness(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["stale"] == "Stale"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_generated_at_is_not_index_freshness_clock(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["stale"] == "Stale"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_stale_threshold_is_45_seconds(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["stale"] == "Stale"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_connection_states_do_not_announce_heartbeats(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["announcements"] == [
        "Connected", "Stale", "Connected", "Unavailable", "Connected",
    ]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_unavailable_snapshot_is_distinct_from_stale_project(tmp_path):
    got = _run_freshness(tmp_path, {})
    assert got["unavailableState"] == "Unavailable"
    assert got["stale"] == "Stale"


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


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_refresh_button_announces_failure_when_indexing_did_not_complete(tmp_path):
    """Codex review finding #9: a failed reindex still returns HTTP 200 with a
    schema-1 envelope -- `response.ok` and `applyDashboardUpdate` both pass it
    -- so without checking `refresh.completed`, the button announced "Updated"
    over data that was never actually refreshed."""
    got = _run_freshness(tmp_path, {})
    assert got["afterFailedRefresh"] == "Refresh failed; existing data kept."


# --- Task 2.4: live.js tolerates the leaf-light Overview DOM ----------------
#
# Overview (`/`) renders totals, a freshness strip, and (at most) a live-status
# word -- it has no `[data-cards-list]`, and the one card-shaped element it
# might address has none of the `[data-burn-*]`/`[data-sparkline]` leaves
# `patchBurn` looks for (those hooks have no renderer on the Overview; git is
# static text in the workspace and has no client-side patcher at all).
# Its freshness strip also never carries `data-generation`/
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
liveWord.attrs["data-live-path"] = "/Users/you/dev/demo";
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
  querySelectorAll(sel) { return selectors[sel] ? [selectors[sel]] : []; },
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
    assert got["freshnessLabel"] == "Connected"
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
    assert got["freshnessLabel"] == "Connected", (
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
  // On /projects the pin sits inside its project's row (`[data-project-card]`),
  // exactly the ancestor Hide keys off; on the detail page it stands alone in
  // the header. `PIN_HAS_CARD` picks which page this run models -- and that
  // ancestor is the whole difference, since only the grouped index re-sorts.
  closest: (sel) => (sel === "[data-project-card]" && PIN_HAS_CARD ? card : null),
};
const list = { appended: 0, append() { this.appended += 1; } };
const cardStatus = { textContent: "" };
const hiddenStatus = { textContent: "" };
const row = { removed: false, remove() { this.removed = true; } };

const nodes = {
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

// The router's no-reload re-render. Present on every real page (router.js loads
// in the persistent shell); stubbed to RECORD the call rather than perform a
// swap, so pin/restore assertions can prove the index re-renders through the
// router -- never a hard reload -- after a change that moves a project between
// sort groups. `HAS_ROUTER=false` models the no-router degradation, where the
// handlers fall back to `location.assign`.
let navigated = null;
if (HAS_ROUTER) {
  globalThis.bridgeNavigate = (href, opts) => {
    navigated = { href, opts: opts === undefined ? null : opts };
    return Promise.resolve();
  };
}

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  querySelector: (sel) => nodes[sel] ?? null,
  // projects.js syncs the list/grid toggle from <html> at load. This harness
  // is about pin/hide, but it evals the same file, so the element the real
  // page always has must exist here too -- stubbing it is right, making the
  // source tolerate a missing documentElement would only hide a real break.
  documentElement: { getAttribute: () => null, setAttribute() {}, removeAttribute() {} },
  querySelectorAll: () => [],
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
    appended: list.appended,
    cardStatus: cardStatus.textContent,
    hiddenStatus: hiddenStatus.textContent,
    rowRemoved: row.removed,
    pressed: pinButton.attrs["aria-pressed"],
    created: created.map((e) => ({ tag: e.tag, href: e.href, className: e.className })),
    assigned,
    navigated,
  }));
});
"""


def _run_projects(
    tmp_path, target: str, ok: bool, pressed: str = "false",
    hide_has_card: bool = True, pin_has_card: bool = True, has_router: bool = True,
) -> dict:
    harness = tmp_path / "projects_harness.js"
    harness.write_text(
        PROJECTS_HARNESS.replace("TARGET", json.dumps(target))
        .replace("PRESSED", json.dumps(pressed))
        .replace("HIDE_HAS_CARD", "true" if hide_has_card else "false")
        .replace("PIN_HAS_CARD", "true" if pin_has_card else "false")
        .replace("HAS_ROUTER", "true" if has_router else "false")
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
    assert got["appended"] == 1
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
    reload would 404, so a successful hide sends the user to /projects. With the
    router present (the real page always loads it) that is a swap, not a hard
    load, so the SSE stream survives."""
    got = _run_projects(tmp_path, "hide", ok=True, hide_has_card=False)
    assert got["navigated"]["href"] == "/projects"
    assert got["assigned"] is None, "took a hard load past the router that was present"
    # Nothing on the workspace to fold into a hidden list, so it does not try.
    assert got["cardRemoved"] is False
    assert got["appended"] == 0


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_hide_on_the_workspace_falls_back_to_a_full_load_without_the_router(tmp_path):
    """The router is progressive enhancement: with it absent, the hide still
    reaches /projects, just via a real navigation rather than a swap."""
    got = _run_projects(tmp_path, "hide", ok=True, hide_has_card=False, has_router=False)
    assert got["assigned"] == "/projects"
    assert got["navigated"] is None


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_refused_hide_leaves_the_card_on_screen_and_says_so(tmp_path):
    """Removing it optimistically would make a 404 indistinguishable from a
    hide that worked, and the project would vanish from a dashboard that had
    not hidden it."""
    got = _run_projects(tmp_path, "hide", ok=False)
    assert got["cardRemoved"] is False
    assert got["appended"] == 0
    assert "⚠" in got["cardStatus"]
    assert any("hiding" in e for e in got["errors"])


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_restore_re_renders_the_index_rather_than_asking_for_a_reload(tmp_path):
    got = _run_projects(tmp_path, "restore", ok=True)
    assert got["sent"]["body"] == {"status": "active"}
    # A restored project comes back as a full card in whatever sort group it now
    # belongs to -- markup only the server renders (rebuilding
    # `project_summary_row` in JS is the duplication the audit called out). So
    # the index re-renders through the router instead of asking for a reload.
    assert got["navigated"] == {"href": "/projects", "opts": {"push": False}}
    assert "reload" not in got["hiddenStatus"]
    assert "✓" in got["hiddenStatus"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_refused_restore_leaves_the_row_in_the_list(tmp_path):
    got = _run_projects(tmp_path, "restore", ok=False)
    assert got["rowRemoved"] is False
    # A refused restore must not re-render -- the row stays and the ⚠ says why.
    assert got["navigated"] is None
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
    # A refused pin changed nothing, so nothing to re-sort -- it must not
    # re-render out from under the \u26a0 message.
    assert got["navigated"] is None


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_pin_on_the_index_re_renders_through_the_router_never_asking_to_reload(tmp_path):
    """The audit's P0: pinning must reflect the new order, not tell the user to
    reload. On /projects the pin sits inside a row, so a successful toggle
    re-renders the grouped index through the router (a swap, not a reload) so
    the row lands in its new sort group from the server's own render."""
    got = _run_projects(tmp_path, "pin", ok=True, pressed="false")
    assert got["navigated"] == {"href": "/projects", "opts": {"push": False}}
    assert "reload" not in got["cardStatus"]
    assert "\u2713 Pinned" in got["cardStatus"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_pin_on_the_detail_page_announces_without_re_rendering(tmp_path):
    """On a project's own detail page the pin stands alone -- there is no list
    to re-sort -- so it announces the new state without navigating away."""
    got = _run_projects(tmp_path, "pin", ok=True, pressed="false", pin_has_card=False)
    assert got["pressed"] == "true"
    assert got["navigated"] is None, "the detail page has no list; it must not navigate away"
    assert "reload" not in got["cardStatus"]
    assert "\u2713 Pinned" in got["cardStatus"]


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

// The list/grid toggle. `data-projects-view` lives on <html> because
// base.html's inline head script sets it pre-paint; the stub mirrors that so
// the load-time sync has the same thing to read that a real page would.
const htmlAttrs = {};
const documentElement = {
  getAttribute(n) { return htmlAttrs[n] ?? null; },
  setAttribute(n, v) { htmlAttrs[n] = v; },
  removeAttribute(n) { delete htmlAttrs[n]; },
};
globalThis.localStorage = {
  store: {},
  getItem(k) { return this.store[k] ?? null; },
  setItem(k, v) { this.store[k] = String(v); },
};
function makeViewButton(name, pressed) {
  return {
    attrs: { "data-projects-view-button": name, "aria-pressed": pressed },
    getAttribute(n) { return this.attrs[n] ?? null; },
    setAttribute(n, v) { this.attrs[n] = v; },
  };
}
const viewList = makeViewButton("list", "true");
const viewGrid = makeViewButton("grid", "false");
const viewButtons = [viewList, viewGrid];

const searchInput = { value: "", focused: false, focus() { this.focused = true; } };
const countNode = { textContent: "" };
const emptyNode = { hidden: true };
// The zero-result state's two halves: the sentence that echoes the query back
// and the control that undoes whatever emptied the list.
const emptyTextNode = { textContent: "" };
const clearNode = { hidden: true };
const listNode = { hidden: false };
const hiddenSection = { hidden: true };

globalThis.document = {
  documentElement,
  addEventListener(type, fn) {
    if (type === "click") clickHandler = fn;
    if (type === "input") inputHandler = fn;
  },
  querySelector(sel) {
    if (sel === "[data-projects-list]") return listNode;
    if (sel === "[data-projects-search]") return searchInput;
    if (sel === "[data-projects-count]") return countNode;
    if (sel === "[data-projects-empty]") return emptyNode;
    if (sel === "[data-projects-empty-text]") return emptyTextNode;
    if (sel === "[data-projects-clear]") return clearNode;
    if (sel === "[data-hidden-projects]") return hiddenSection;
    if (sel === '[data-projects-filter][aria-pressed="true"]') {
      return filterButtons.find((b) => b.attrs["aria-pressed"] === "true") || null;
    }
    return null;
  },
  querySelectorAll(sel) {
    if (sel === "[data-project-row-item]") return rows;
    if (sel === "[data-projects-filter]") return filterButtons;
    if (sel === "[data-projects-view-button]") return viewButtons;
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

// The way out of the zero-result state. Runs last so it undoes whatever the
// query and filter above did, which is exactly the situation it exists for.
if (CLEAR_CLICK) {
  clickHandler({ target: { closest: (sel) => (sel === "[data-projects-clear]" ? clearNode : null) } });
}

if (VIEW_CLICK) {
  const target = viewButtons.find((b) => b.attrs["data-projects-view-button"] === VIEW_CLICK);
  clickHandler({ target: { closest: (sel) => (sel === "[data-projects-view-button]" ? target : null) } });
}

console.log(JSON.stringify({
  rowHidden: rows.map((r) => r.hidden),
  hiddenRowHidden: hiddenRows.map((r) => r.hidden),
  count: countNode.textContent,
  emptyHidden: emptyNode.hidden,
  emptyText: emptyTextNode.textContent,
  clearHidden: clearNode.hidden,
  listHidden: listNode.hidden,
  hiddenSectionHidden: hiddenSection.hidden,
  pressed: filterButtons.map((b) => b.attrs["aria-pressed"]),
  searchValue: searchInput.value,
  searchFocused: searchInput.focused,
  viewAttr: htmlAttrs["data-projects-view"] ?? null,
  viewStored: localStorage.getItem("bridge.projectsView"),
  viewPressed: viewButtons.map((b) => b.attrs["aria-pressed"]),
  fetchCalled,
}));
"""


def _run_projects_filter(tmp_path, query: str, filter_target, clear_click: bool = False,
                         view_click=None, stored_view=None):
    harness = tmp_path / "projects_filter_harness.js"
    target_literal = json.dumps(filter_target) if filter_target is not None else "null"
    script = (
        PROJECTS_FILTER_HARNESS.replace("QUERY", json.dumps(query))
        .replace("FILTER_TARGET", target_literal)
        .replace("CLEAR_CLICK", "true" if clear_click else "false")
        .replace("VIEW_CLICK", json.dumps(view_click) if view_click else "null")
    )
    if stored_view is not None:
        # Stands in for base.html's inline head script, which sets the
        # attribute pre-paint from localStorage before projects.js ever runs.
        script = script.replace(
            "const fs = require",
            f'htmlAttrs["data-projects-view"] = {json.dumps(stored_view)};\nconst fs = require',
        )
    harness.write_text(script)
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
def test_projects_empty_state_echoes_the_query_back(tmp_path):
    """A zero-result state that does not repeat the query leaves the user unable
    to tell a typo from a genuinely absent project, so the exact string typed is
    echoed -- and the escape hatch appears alongside it."""
    got = _run_projects_filter(tmp_path, "zzz-nope", None)
    assert got["emptyHidden"] is False
    assert got["emptyText"] == 'No projects match "zzz-nope".'
    assert got["clearHidden"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_projects_empty_state_names_the_filter_when_a_query_is_also_active(tmp_path):
    """Zero-with-a-filter needs a different sentence from zero-with-a-query
    alone, because the way out is different: the filter is the more likely
    culprit and the user cannot see it in the query box. The `hidden` filter
    also proves the sentence names WHICH list came up empty.

    (The third branch -- a filter emptying the list with no query at all -- is
    not reachable from this fixture: every filter it defines matches at least
    one of the three rows. It is exercised in the real app by e.g. `Running` at
    zero live sessions.)"""
    got = _run_projects_filter(tmp_path, "zzz-nope", "hidden")
    assert got["hiddenRowHidden"] == [True]
    assert got["count"] == "0 projects shown"
    assert got["emptyHidden"] is False
    assert got["emptyText"] == 'No hidden projects match "zzz-nope" in this filter.'
    assert got["clearHidden"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_projects_clear_resets_both_the_query_and_the_filter_without_fetching(tmp_path):
    """Either input alone can be what emptied the list, so Clear resets both --
    clearing only one would land the user on a second empty page. Still no
    fetch and no reload, the invariant this whole file exists to protect."""
    got = _run_projects_filter(tmp_path, "zzz-nope", "running", clear_click=True)
    assert got["searchValue"] == ""
    assert got["pressed"] == ["true", "false", "false", "false", "false"]
    assert got["rowHidden"] == [False, False, False]
    assert got["count"] == "3 projects shown"
    assert got["emptyHidden"] is True
    assert got["clearHidden"] is True
    assert got["searchFocused"] is True
    assert got["fetchCalled"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_projects_grid_toggle_sets_the_attribute_persists_it_and_never_fetches(tmp_path):
    """The layout is pure CSS off `data-projects-view`; this only moves the
    attribute, keeps both buttons' `aria-pressed` agreeing with it, and stores
    the choice -- Bridge is server-rendered, so a layout held only in memory
    would revert on the next nav click."""
    got = _run_projects_filter(tmp_path, "", None, view_click="grid")
    assert got["viewAttr"] == "grid"
    assert got["viewStored"] == "grid"
    assert got["viewPressed"] == ["false", "true"]
    # Switching layout must not disturb the list itself or hit the network.
    assert got["rowHidden"] == [False, False, False]
    assert got["fetchCalled"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_projects_grid_toggle_back_to_list_removes_the_attribute(tmp_path):
    """List is the absence of the attribute, not a second value -- so going
    back has to REMOVE it, or the CSS would keep matching the grid rules."""
    got = _run_projects_filter(tmp_path, "", None, view_click="list", stored_view="grid")
    assert got["viewAttr"] is None
    assert got["viewStored"] == "list"
    assert got["viewPressed"] == ["true", "false"]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_projects_view_buttons_resync_from_the_persisted_attribute_on_load(tmp_path):
    """The template always renders List as pressed, but the inline head script
    may already have set grid. Without the load-time resync the control would
    announce "List, pressed" over a grid -- a glyph-and-word toggle whose state
    lies is worse than no toggle."""
    got = _run_projects_filter(tmp_path, "", None, stored_view="grid")
    assert got["viewAttr"] == "grid"
    assert got["viewPressed"] == ["false", "true"]


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
    if (name === "data-compose-path") return "/Users/you/dev/demo";
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
        "launch-1", "/Users/you/dev/demo",
    ]
    assert got["sentBody"] == {
        "project_path": "/Users/you/dev/demo",
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
const statusNode = { textContent: "" };

const panel = {
  hidden: false,
  getAttribute(name) {
    if (name === "data-schedule-path") return "/Users/you/dev/demo";
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
    if (sel === '[data-schedule-status="schedule-panel-1"]') return statusNode;
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
  return { ok: true, status: 201, json: async () => ({ id: "new-job", journaled: JOURNALED }) };
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const submitButton = {
  getAttribute: () => "schedule-panel-1",
  closest: (sel) => (sel === "[data-schedule-submit]" ? submitButton : null),
  disabled: false,
};

clickHandler({ target: submitButton }).then(() => {
  console.log(JSON.stringify({ sentUrl, sentMethod, sentBody, status: statusNode.textContent }));
});
"""


def _run_schedule_submit(tmp_path, handoff_id, journaled=True) -> dict:
    script = (
        SCHEDULE_SUBMIT_HARNESS
        .replace("HANDOFF_ID", json.dumps(handoff_id))
        .replace("JOURNALED", json.dumps(journaled))
    )
    return _run_node(tmp_path, "schedule_submit.js", script, SCHEDULE_JS)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_schedule_submit_posts_seconds_and_a_null_source_for_the_compose_box(tmp_path):
    got = _run_schedule_submit(tmp_path, handoff_id=None)
    assert got["sentUrl"] == "/api/schedule"
    assert got["sentMethod"] == "POST"
    body = got["sentBody"]
    assert body["project_path"] == "/Users/you/dev/demo"
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


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_schedule_submit_announces_plain_success_when_journaled(tmp_path):
    got = _run_schedule_submit(tmp_path, handoff_id=None, journaled=True)
    assert got["status"] == "✓ Scheduled"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_schedule_submit_warns_when_the_journal_write_failed(tmp_path):
    """Codex review finding #10: a journal failure never costs the schedule
    itself (the row exists and fires normally), but the panel announced a
    plain "✓ Scheduled" regardless -- claiming a durability the write never
    achieved, with nothing telling the user a database loss could lose it."""
    got = _run_schedule_submit(tmp_path, handoff_id=None, journaled=False)
    assert got["status"] == "⚠ Scheduled, but not saved durably — a database reset could lose it"


# --- Codex review finding #15: a programmatic clear must not leave the
#     compose draft or the launch button stale ------------------------------
#
# `field.value = ""` fires no `input` event, so `saveComposeDraft` (the
# sessionStorage persistence) and the launch button's enable rule -- both
# wired to that event in launch.js -- never learn the field emptied. The old
# prompt could resurface on the next router swap, and the button stayed
# enabled over nothing. `schedule.js` now routes both clears through
# `window.bridgeClearComposeField` (launch.js), so this loads BOTH files, as
# the settings.js + launch.js harness above does, and drives the real
# cross-file call rather than a stub.
COMPOSE_CLEAR_HARNESS = """
globalThis.window = globalThis;
// schedule.js AND launch.js each register their own delegated "click"
// listener -- a single `clickHandler = fn` slot (fine when only one file is
// loaded) silently drops the first registration to the second file's `eval`.
// Every listener must fire, exactly as real `addEventListener` calls would.
let clickHandlers = [];
async function clickHandler(event) {
  for (const fn of clickHandlers) await fn(event);
}

const composeField = {
  id: "compose-1", value: "typed prompt", dataset: {},
  closest: (sel) => (sel === "[data-compose-prompt]" ? composeField : null),
};
const launchButton = {
  disabled: false,
  attrs: { "data-launch-button": "launch-1" },
  getAttribute(n) { return this.attrs[n] ?? null; },
  setAttribute() {}, removeAttribute() {},
  closest: (sel) => (sel === "[data-launch-button]" ? launchButton : null),
};
const launchBand = {
  attrs: { "data-launch-prompt": "compose-1" },
  getAttribute(n) {
    return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null;
  },
  querySelector: (sel) => (sel === "[data-launch-button]" ? launchButton : null),
};
const statusNode = { textContent: "" };

const storageMap = new Map();
storageMap.set("bridge.compose.compose-1", "typed prompt");
globalThis.sessionStorage = {
  getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
};

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandlers.push(fn); },
  getElementById: (id) => (id === "compose-1" ? composeField : null),
  querySelector(sel) {
    if (sel === '[data-compose-status="compose-1"]') return statusNode;
    if (sel === '[data-schedule-status="schedule-panel-1"]') return statusNode;
    if (sel === '[data-launch-prompt="compose-1"]') return launchBand;
    return null;
  },
  querySelectorAll: () => [],
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.fetch = async () => (
  { ok: true, status: 200, json: async () => ({ outcome: "started" }) }
);

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));  // schedule.js
eval(fs.readFileSync(process.argv[3], "utf8"));  // launch.js

(async () => {
  TARGET

  console.log(JSON.stringify({
    fieldValue: composeField.value,
    storage: Object.fromEntries(storageMap),
    buttonDisabled: launchButton.disabled,
  }));
})();
"""


def _run_compose_clear(tmp_path, name: str, target: str) -> dict:
    harness = tmp_path / f"compose_clear_{name}.js"
    harness.write_text(COMPOSE_CLEAR_HARNESS.replace("TARGET", target))
    proc = subprocess.run(
        [_node(), str(harness), str(SCHEDULE_JS), str(LAUNCH_JS)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_successful_compose_launch_clears_the_stale_draft_and_disables_the_button(
    tmp_path,
):
    runButtonScript = """
    const runButton = {
      disabled: false,
      setAttribute() {}, removeAttribute() {},
      getAttribute(name) {
        if (name === "data-compose-run") return "compose-1";
        if (name === "data-compose-path") return "/Users/you/dev/demo";
        if (name === "data-compose-launch") return "launch-1";
        return null;
      },
      closest(sel) { return sel === "[data-compose-run]" ? runButton : null; },
    };
    await clickHandler({ target: runButton });
    """
    got = _run_compose_clear(tmp_path, "launch", runButtonScript)
    assert got["fieldValue"] == ""
    assert got["storage"] == {}
    assert got["buttonDisabled"] is True


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_successful_schedule_submit_clears_the_stale_draft_and_disables_the_button(
    tmp_path,
):
    submitScript = """
    const whenInput = { value: "2026-06-01T10:00" };
    const modeSelect = { value: "terminal" };
    const toggleButton = { focus() {}, setAttribute() {}, getAttribute: () => null };
    const panel = {
      hidden: false,
      getAttribute(name) {
        if (name === "data-schedule-path") return "/Users/you/dev/demo";
        if (name === "data-schedule-prompt") return "compose-1";
        if (name === "data-schedule-handoff") return null;
        return null;
      },
      querySelector(sel) {
        if (sel === "[data-schedule-when]") return whenInput;
        if (sel === "[data-schedule-mode]") return modeSelect;
        return null;
      },
    };
    document.getElementById = (id) => {
      if (id === "schedule-panel-1") return panel;
      if (id === "compose-1") return composeField;
      return null;
    };
    const originalQuerySelector = document.querySelector.bind(document);
    document.querySelector = (sel) => {
      if (sel === '[data-schedule-toggle="schedule-panel-1"]') return toggleButton;
      if (sel === "[data-scheduled-count]") return null;
      if (sel === "[data-topbar-scheduled]") return null;
      return originalQuerySelector(sel);
    };
    globalThis.fetch = async () => ({ ok: true, status: 201, json: async () => ({ id: "job-1" }) });
    const submitButton = {
      getAttribute: () => "schedule-panel-1",
      closest: (sel) => (sel === "[data-schedule-submit]" ? submitButton : null),
      disabled: false,
    };
    await clickHandler({ target: submitButton });
    """
    got = _run_compose_clear(tmp_path, "schedule", submitScript)
    assert got["fieldValue"] == ""
    assert got["storage"] == {}
    assert got["buttonDisabled"] is True


# --- Codex review finding #16: editing a schedule must update the `<time>`
#     element's machine-readable `datetime`, not just its visible text -------
#
# The edit-save handler updated `data-scheduled-for` (which drives the
# repainted VISIBLE text) and the input's own `data-scheduled-epoch`, but
# never touched the `<time>` element's `datetime` attribute -- the one a
# screen reader or any other consumer of the real HTML semantics actually
# reads. A successful edit left it naming the time the row was edited AWAY
# from, forever.
SCHEDULE_EDIT_SAVE_HARNESS = """
globalThis.window = globalThis;
let clickHandler = null;

const timeEl = {
  attrs: { "data-scheduled-for": "1000" },
  setAttribute(name, value) { this.attrs[name] = String(value); },
  getAttribute(name) { return this.attrs[name] ?? null; },
  textContent: "",
};
const whenInput = {
  value: "2026-06-01T10:00",
  attrs: { "data-scheduled-epoch": "1000" },
  setAttribute(name, value) { this.attrs[name] = String(value); },
  getAttribute(name) { return this.attrs[name] ?? null; },
};
const statusNode = { textContent: "" };
const row = {
  querySelector(sel) { return sel === "[data-scheduled-for]" ? timeEl : null; },
  querySelectorAll: () => [],
};

globalThis.document = {
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  querySelector(sel) {
    if (sel === '[data-scheduled-edit-when="j1"]') return whenInput;
    if (sel === '[data-scheduled-job="j1"]') return row;
    if (sel === '[data-scheduled-status="j1"]') return statusNode;
    return null;
  },
};
globalThis.fetch = async () => ({
  ok: true, status: 200, json: async () => ({ id: "j1", scheduled_for: 1717200000 }),
});

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const editSave = {
  getAttribute: () => "j1",
  closest: (sel) => (sel === "[data-scheduled-edit-save]" ? editSave : null),
  disabled: false,
};

clickHandler({ target: editSave }).then(() => {
  console.log(JSON.stringify({
    dataScheduledFor: timeEl.getAttribute("data-scheduled-for"),
    datetime: timeEl.getAttribute("datetime"),
    status: statusNode.textContent,
  }));
});
"""


def _run_schedule_edit_save(tmp_path) -> dict:
    return _run_node(tmp_path, "schedule_edit_save.js", SCHEDULE_EDIT_SAVE_HARNESS, SCHEDULE_JS)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_editing_a_schedule_updates_the_datetime_attribute_not_just_the_text(tmp_path):
    # "2026-06-01T10:00" read as UTC (the harness runs under TZ=UTC, matching
    # test_schedule_submit_posts_seconds_and_a_null_source_for_the_compose_box's
    # own use of the same input string and expected epoch).
    got = _run_schedule_edit_save(tmp_path)
    assert got["dataScheduledFor"] == "1780308000"
    assert got["datetime"] is not None
    # The attribute must actually name the NEW time, not be merely present.
    assert datetime.fromisoformat(
        got["datetime"].replace("Z", "+00:00")
    ).timestamp() == 1780308000
    assert got["status"] == "✓ Saved"


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
// The rail's collapse control. Matches ONLY its own selector so the two
// disclosures can be exercised independently through the one click handler.
const rail = {
  attrs: {
    "aria-expanded": "true", "aria-controls": "primary-nav",
    "aria-label": "Collapse sidebar", "title": "Collapse sidebar",
  },
  getAttribute(name) { return this.attrs[name] ?? null; },
  setAttribute(name, value) { this.attrs[name] = value; },
  closest(sel) { return sel === "[data-sidebar-toggle]" ? this : null; },
};
const documentElement = {
  attrs: __DATA_NAV__,
  getAttribute(name) { return this.attrs[name] ?? null; },
  setAttribute(name, value) { this.attrs[name] = value; },
  removeAttribute(name) { delete this.attrs[name]; },
};
const store = {};
globalThis.localStorage = {
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
  },
  setItem(key, value) { store[key] = value; },
  removeItem(key) { delete store[key]; },
};
let clickHandler = null;
globalThis.document = {
  documentElement,
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  getElementById: (id) => (id === "primary-nav" ? nav : null),
  querySelectorAll: (sel) => (sel === "[data-sidebar-toggle]" ? [rail] : []),
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const beforeExpanded = button.attrs["aria-expanded"];
const beforeHidden = nav.hasAttribute("hidden");
const loadRailExpanded = rail.attrs["aria-expanded"];
const loadRailLabel = rail.attrs["aria-label"];
const loadRailTitle = rail.attrs["title"];
const loadStored = globalThis.localStorage.getItem("bridge.nav");

clickHandler({ target: button });
const afterFirstClickExpanded = button.attrs["aria-expanded"];
const afterFirstClickHidden = nav.hasAttribute("hidden");

clickHandler({ target: button });
const afterSecondClickExpanded = button.attrs["aria-expanded"];
const afterSecondClickHidden = nav.hasAttribute("hidden");

clickHandler({ target: rail });
const railNav1 = documentElement.getAttribute("data-nav");
const railExpanded1 = rail.attrs["aria-expanded"];
const railLabel1 = rail.attrs["aria-label"];
const railStored1 = globalThis.localStorage.getItem("bridge.nav");
const railNavHidden1 = nav.hasAttribute("hidden");

clickHandler({ target: rail });
const railNav2 = documentElement.getAttribute("data-nav");
const railExpanded2 = rail.attrs["aria-expanded"];
const railLabel2 = rail.attrs["aria-label"];
const railStored2 = globalThis.localStorage.getItem("bridge.nav");

console.log(JSON.stringify({
  beforeExpanded, beforeHidden,
  loadRailExpanded, loadRailLabel, loadRailTitle, loadStored,
  afterFirstClickExpanded, afterFirstClickHidden,
  afterSecondClickExpanded, afterSecondClickHidden,
  railNav1, railExpanded1, railLabel1, railStored1, railNavHidden1,
  railNav2, railExpanded2, railLabel2, railStored2,
}));
"""


def _run_shell_harness(tmp_path, name, data_nav="{}"):
    harness = tmp_path / name
    harness.write_text(SHELL_HARNESS.replace("__DATA_NAV__", data_nav))
    proc = subprocess.run(
        [_node(), str(harness), str(SHELL_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_shell_js_does_not_touch_the_nav_on_load(tmp_path):
    """With no JS -- or before any click -- the nav is exactly as the server
    rendered it: visible, `hidden` absent. Loading shell.js must not itself
    collapse the nav; only a click may.

    NARROWED (sidebar collapse): shell.js is no longer strictly inert on load.
    It now syncs the rail toggle's `aria-expanded`/`aria-label`/`title` to the
    persisted `data-nav`, because that state is a localStorage preference the
    server cannot see -- without the sync, loading a page with the rail already
    collapsed would announce "Collapse sidebar, expanded" to a screen reader
    while the rail sat visibly collapsed. The invariant this test defends is
    unchanged and is the one that matters: the load-time work touches only the
    toggle's own attributes, never the nav's visibility, and never storage.
    """
    got = _run_shell_harness(tmp_path, "shell_load_harness.js")
    assert got["beforeHidden"] is False
    assert got["beforeExpanded"] == "true"
    # Load must not write a preference the user never expressed.
    assert got["loadStored"] is None


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_shell_js_menu_toggle_collapses_and_restores_the_nav(tmp_path):
    got = _run_shell_harness(tmp_path, "shell_toggle_harness.js")
    assert got["afterFirstClickExpanded"] == "false"
    assert got["afterFirstClickHidden"] is True
    assert got["afterSecondClickExpanded"] == "true"
    assert got["afterSecondClickHidden"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_shell_js_rail_toggle_collapses_persists_and_renames_itself(tmp_path):
    """The rail collapse must do three things per click, or it is broken in a
    way no HTML inspection would reveal: flip `data-nav` (what CSS reads),
    write `bridge.nav` (what survives the next full page load -- every nav
    click in Bridge is one), and rename itself, since a glyph-only control's
    `aria-label` IS its only name and a stale one lies about the state.

    It must also NOT reach for the nav's `hidden` attribute: the collapsed rail
    hides the nav in CSS, and setting `hidden` here too would leave the
    `.menu-toggle` path fighting it at the next breakpoint change.
    """
    got = _run_shell_harness(tmp_path, "shell_rail_harness.js")

    assert got["railNav1"] == "collapsed"
    assert got["railStored1"] == "collapsed"
    assert got["railExpanded1"] == "false"
    assert got["railLabel1"] == "Expand sidebar"
    assert got["railNavHidden1"] is False, "the rail path must not set nav[hidden]"

    assert got["railNav2"] is None, "expanding must remove data-nav, not blank it"
    assert got["railStored2"] == "expanded"
    assert got["railExpanded2"] == "true"
    assert got["railLabel2"] == "Collapse sidebar"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_shell_js_reports_a_persisted_collapse_on_load(tmp_path):
    """Loading with `data-nav="collapsed"` already applied (base.html's inline
    head script does that pre-paint) must leave the toggle describing the state
    it is actually in -- collapsed, and offering to expand."""
    got = _run_shell_harness(
        tmp_path, "shell_collapsed_harness.js", data_nav='{"data-nav": "collapsed"}',
    )
    assert got["loadRailExpanded"] == "false"
    assert got["loadRailLabel"] == "Expand sidebar"
    assert got["loadRailTitle"] == "Expand sidebar"
    # Still no storage write, and still no opinion about the nav itself.
    assert got["loadStored"] is None
    assert got["beforeHidden"] is False


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

// Real `<select>`s are their own `closest(selector)` match (they have no
// parents here), which is exactly what the delegated `change` listener in
// settings.js relies on to route an event back to the right key.
function makeSelect(initialValue, selector) {
  return {
    value: initialValue,
    selector,
    closest(sel) { return sel === this.selector ? this : null; },
  };
}

const selects = {
  '[data-settings-theme]': makeSelect("system", '[data-settings-theme]'),
  '[data-settings-density]': makeSelect("comfortable", '[data-settings-density]'),
  '[data-settings-launch-model]': makeSelect("opus", '[data-settings-launch-model]'),
  '[data-settings-launch-effort]': makeSelect("low", '[data-settings-launch-effort]'),
  '[data-settings-launch-mode]': makeSelect("", '[data-settings-launch-mode]'),
  // Present in the harness so a mistaken query for the LIVE launch band's
  // permission control would resolve to something observable instead of
  // silently returning null and masking the bug.
  '[data-launch-perm="launch-1"]': makeSelect("bypassPermissions", '[data-launch-perm="launch-1"]'),
};

const queried = [];
const documentChangeListeners = [];
globalThis.document = {
  documentElement,
  querySelector(sel) { queried.push(sel); return selects[sel] ?? null; },
  addEventListener(type, fn) { if (type === "change") documentChangeListeners.push(fn); },
};

// Same queue-and-run shape as the real registry in shell.js. Its presence
// (not just its shape) matters: shell.js loads first in every real page and
// unconditionally defines `window.bridgePage`, so settings.js's
// `if (window.bridgePage) ... else ...` guard always takes the `if` branch
// in a browser -- a harness that leaves `bridgePage` undefined only ever
// exercises the `else` fallback, which is not the path production code takes.
const enterHooks = [];
window.bridgePage = {
  onEnter(fn) { enterHooks.push(fn); },
  enter() { for (const fn of enterHooks) fn(); },
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

// Simulate shell.js's task-3 bootstrap: the browser calls `enter()` once for
// the page's first view, after every deferred script (including this one)
// has registered its hooks. Without this call, `restoreSettingsSelects`
// would be queued but never run, and every assertion below would be
// observing a page that never actually restored anything.
window.bridgePage.enter();

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
            'documentChangeListeners.forEach((fn) => fn({ target: el }));'
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
  getAttribute(name) { return name === "data-launch-path" ? "/Users/you/dev/demo" : null; },
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

const body = window.bridgeLaunchBody("launch-1", "/Users/you/dev/demo");

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
    getAttribute: (name) => (name === "data-launch-path" ? "/Users/you/dev/demo" : null),
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

const body = window.bridgeLaunchBody("launch-1", "/Users/you/dev/demo");

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


# --- the command strip's numbers are the ones on screen ----------------------
#
# The Overview renders every shared total TWICE: once in the six-cell command
# strip that is actually visible, once in the metrics list inside a collapsed
# `<details>`. live.js used to resolve each hook with `querySelector`, so it
# patched whichever came first in the document -- the hidden one -- and the
# visible number sat at its page-load value for as long as the tab stayed open.
#
# The `attention` and `dirty` cells have no twin at all; they were on the wire
# with nothing reading them.

COMMAND_STRIP_HARNESS = r'''
globalThis.window = globalThis;
globalThis.CSS = { escape: (s) => s };
Date.now = () => 100000;

function classes() {
  const values = new Set();
  return {
    add: (...names) => names.forEach((name) => values.add(name)),
    remove: (...names) => names.forEach((name) => values.delete(name)),
    toggle(name, force) { if (force) values.add(name); else values.delete(name); },
    has: (name) => values.has(name),
    values: () => [...values],
  };
}
function node(attrs = {}) {
  return {
    attrs: { ...attrs }, hidden: false, textContent: "", classList: classes(),
    getAttribute(name) { return this.attrs[name] ?? null; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    removeAttribute(name) { delete this.attrs[name]; },
    querySelector() { return null; },
    closest() { return null; },
  };
}

// A strip cell: the number, wrapped in a cell that names its own flag class.
function cell(flag, lit) {
  const wrapper = node(flag ? { "data-cell-flag": flag } : {});
  if (lit) wrapper.classList.add(flag);
  const num = node();
  num.closest = (sel) => (sel === "[data-cell-flag]" && flag ? wrapper : null);
  return { wrapper, num };
}

const running = cell("is-live", false);
const attention = cell("is-hot", true);   // server rendered it lit, at 3
attention.num.textContent = "3";
const dirty = cell(null, false);
// The hidden twins inside the collapsed <details>.
const runningDd = node();
const projectsDd = node();

const bySelector = {
  '[data-dashboard-total="running"]': [running.num, runningDd],
  '[data-dashboard-total="attention"]': [attention.num],
  '[data-dashboard-total="dirty"]': [dirty.num],
  '[data-dashboard-total="projects"]': [projectsDd],
};

globalThis.document = {
  addEventListener() {},
  querySelector(sel) { return (bySelector[sel] || [])[0] || null; },
  querySelectorAll(sel) { return bySelector[sel] || []; },
};
globalThis.EventSource = class { addEventListener() {} close() {} };
globalThis.setTimeout = (fn) => { fn(); return 0; };
window.setTimeout = globalThis.setTimeout;
globalThis.setInterval = () => 0;
globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

window.bridgeApplyDashboardUpdate({
  schema: 1, kind: "snapshot", generated_at: 1, generation: 1,
  freshness: { server: "available", index_at: 100, index_age_seconds: 0 },
  topbar: { running: 2, attention: 0, dirty: 5, projects: 9 },
  diagnostics: { alert: false }, card_order: [], cards: {},
  refresh: { attempted: false, completed: true, error: null }, unattributed: [],
});

console.log(JSON.stringify({
  stripRunning: running.num.textContent,
  hiddenRunning: runningDd.textContent,
  stripAttention: attention.num.textContent,
  stripDirty: dirty.num.textContent,
  hiddenProjects: projectsDd.textContent,
  runningLit: running.wrapper.classList.has("is-live"),
  attentionLit: attention.wrapper.classList.has("is-hot"),
}));
'''


def _run_command_strip(tmp_path):
    harness = tmp_path / "command_strip_harness.js"
    harness.write_text(COMMAND_STRIP_HARNESS)
    proc = subprocess.run(
        [_node(), str(harness), str(LIVE_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_total_rendered_twice_is_patched_in_both_places(tmp_path):
    got = _run_command_strip(tmp_path)
    assert got["stripRunning"] == "2", (
        "the visible command-strip number was left at its page-load value"
    )
    assert got["hiddenRunning"] == "2"
    assert got["hiddenProjects"] == "9"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_the_strip_only_totals_are_patched_at_all(tmp_path):
    """`attention` and `dirty` render only in the strip. Both were already in
    the `topbar` payload before the strip carried hooks, read by nothing."""
    got = _run_command_strip(tmp_path)
    assert got["stripAttention"] == "0"
    assert got["stripDirty"] == "5"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_cells_colour_follows_the_number_it_was_given(tmp_path):
    """A cell colours itself from its own count. Patching the text without the
    class leaves "Needs attention" lit over a 0 -- a warning for nothing, and
    the same colour-contradicts-the-word failure the attention pill had."""
    got = _run_command_strip(tmp_path)
    assert got["attentionLit"] is False, "the cell stayed lit over a count of 0"
    assert got["runningLit"] is True, "a cell that went from 0 to 2 stayed cold"


# --- shell.js: widening the window must not strand the nav -------------------

SHELL_RESIZE_HARNESS = r'''
globalThis.window = globalThis;
const nav = {
  attrs: {},
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); },
  setAttribute(name, value) { this.attrs[name] = value; },
  removeAttribute(name) { delete this.attrs[name]; },
};
const menu = {
  attrs: { "aria-expanded": "true", "aria-controls": "primary-nav" },
  getAttribute(name) { return this.attrs[name] ?? null; },
  setAttribute(name, value) { this.attrs[name] = value; },
  closest(sel) { return sel === ".menu-toggle" ? this : null; },
};
const documentElement = {
  attrs: {},
  getAttribute(name) { return this.attrs[name] ?? null; },
  setAttribute(name, value) { this.attrs[name] = value; },
  removeAttribute(name) { delete this.attrs[name]; },
};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };

// Start narrow: the rail query does not match, so `.menu-toggle` is the
// visible disclosure and `.sidebar-toggle` is display:none.
let railMatches = false;
const railListeners = [];
globalThis.window.matchMedia = (query) => ({
  get matches() { return railMatches; },
  media: query,
  addEventListener(type, fn) { if (type === "change") railListeners.push(fn); },
});

let clickHandler = null;
globalThis.document = {
  documentElement,
  addEventListener(type, fn) { if (type === "click") clickHandler = fn; },
  getElementById: (id) => (id === "primary-nav" ? nav : null),
  querySelector: (sel) => (sel === ".menu-toggle" ? menu : null),
  querySelectorAll: () => [],
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

// Collapse the nav at narrow width, the only width where the button exists.
clickHandler({ target: menu });
const narrowHidden = nav.hasAttribute("hidden");
const narrowExpanded = menu.attrs["aria-expanded"];

// Widen past the rail breakpoint. CSS now hides `.menu-toggle` outright, so
// nothing on screen can undo the collapse.
railMatches = true;
railListeners.forEach((fn) => fn({ matches: true }));

console.log(JSON.stringify({
  narrowHidden, narrowExpanded,
  listeners: railListeners.length,
  wideHidden: nav.hasAttribute("hidden"),
  wideExpanded: menu.attrs["aria-expanded"],
}));
'''


def _run_shell_resize(tmp_path):
    harness = tmp_path / "shell_resize_harness.js"
    harness.write_text(SHELL_RESIZE_HARNESS)
    proc = subprocess.run(
        [_node(), str(harness), str(SHELL_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_widening_past_the_rail_breakpoint_restores_the_nav(tmp_path):
    """`.menu-toggle` hides the nav with the `hidden` attribute, and CSS drops
    the button itself at 1024px. Collapse the nav on a narrow window, widen it,
    and the nav stayed `hidden` with nothing left on screen able to bring it
    back -- while `.sidebar-toggle`, now the visible control, claimed
    `aria-expanded="true"` over a nav that was not there.
    """
    got = _run_shell_resize(tmp_path)

    assert got["listeners"] == 1, "no breakpoint listener was registered"
    assert got["narrowHidden"] is True
    assert got["narrowExpanded"] == "false"

    assert got["wideHidden"] is False, "the nav stayed hidden with no way back"
    assert got["wideExpanded"] == "true", (
        "the Menu button still claims the nav is collapsed"
    )


# --- Compose-box draft persistence across a router swap ---------------------
#
# The compose textarea (`_launch.html`'s ad hoc "start a new session" prompt,
# `id="compose-<project_id>"`) has no server-side record -- unlike the handoff
# prompt, which `savePrompt` above flushes to the server on `onLeave`. A
# navigation swap (`.shell__body` replaced via the router) destroys the field
# with nothing to restore it from, so typed text was silently lost. The fix is
# client-side only: `sessionStorage` under `bridge.compose.<field.id>`, saved
# on every `input` and restored on `bridgePage.onEnter`. `sessionStorage`
# (never `localStorage`) is the point -- the draft must not survive a browser
# restart, only in-tab navigation and reload.
#
# The harness reuses EMPTY_STATE_HARNESS's compose/band/button shape (this is
# the same field the empty-state "Start session" band points at) and adds a
# `sessionStorage` stub keyed by a Map, with an optional THROW flag so the
# storage-failure tests can prove `getItem`/`setItem` throwing is a silent
# no-op rather than a broken handler. `window.bridgePage` is stubbed with the
# same queue-and-run shape `SETTINGS_HARNESS` uses, so `onEnter(fn)` queues and
# `enter()` fires every queued hook once -- the same call shell.js makes for
# the page's first view and every swap after it.
COMPOSE_DRAFT_HARNESS = """
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
  attrs: { "data-launch": "launch-1", "data-launch-path": "/Users/you/dev/demo",
           "data-launch-prompt": "compose-1" },
  getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; },
  querySelector: (sel) => (sel === "[data-launch-button]" ? button : null),
};
const controls = {
  '[data-launch="launch-1"]': band,
  '[data-launch-prompt="compose-1"]': band,
};
globalThis.document = {
  addEventListener(type, fn) {
    if (type === "click") clickHandlers.push(fn);
    if (type === "input") inputHandlers.push(fn);
  },
  getElementById: (id) => (id === "compose-1" ? composeField : null),
  querySelector: (sel) => controls[sel] ?? null,
  querySelectorAll: (sel) =>
    sel === "[data-compose-prompt]" ? [composeField] : [],
  createRange: () => ({}),
};
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });

// A Map-backed sessionStorage stub, matching minidom.js's stub shape, with an
// optional THROW switch the failure tests flip on.
let throwOnAccess = THROW;
const storageMap = new Map();
globalThis.sessionStorage = {
  getItem(k) {
    if (throwOnAccess) throw new Error("blocked");
    return storageMap.has(k) ? storageMap.get(k) : null;
  },
  setItem(k, v) {
    if (throwOnAccess) throw new Error("blocked");
    storageMap.set(k, String(v));
  },
  removeItem(k) {
    if (throwOnAccess) throw new Error("blocked");
    storageMap.delete(k);
  },
};
// Preload a draft for the restore tests, from Python-supplied JSON.
for (const [k, v] of Object.entries(PRELOAD)) storageMap.set(k, v);

const enterHooks = [];
window.bridgePage = {
  onEnter(fn) { enterHooks.push(fn); },
  // launch.js also registers an `onLeave` hook (the handoff-prompt flush);
  // this harness never calls it, but a real page always defines it, so it
  // must exist or `window.bridgePage.onLeave(...)` throws at load time.
  onLeave() {},
  enter() { for (const fn of enterHooks) fn(); },
};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

const fireInput = () => Promise.all(inputHandlers.map((fn) =>
  fn({ target: { closest: (sel) => composeField.closest(sel) } })));

(async () => {
  SCRIPT
  console.log(JSON.stringify({
    storage: Object.fromEntries(storageMap),
    fieldValue: composeField.value,
    buttonDisabled: button.disabled,
  }));
})();
"""


def _run_compose_draft(tmp_path, script: str, preload: dict, throw: bool = False) -> dict:
    harness = tmp_path / "compose_draft_harness.js"
    text = (
        COMPOSE_DRAFT_HARNESS
        .replace("SCRIPT", script)
        .replace("PRELOAD", json.dumps(preload))
        .replace("THROW", "true" if throw else "false")
    )
    harness.write_text(text)
    proc = subprocess.run(
        [_node(), str(harness), str(LAUNCH_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_typing_into_the_compose_field_persists_the_draft_to_session_storage(tmp_path):
    got = _run_compose_draft(tmp_path, """
    composeField.value = "half-written prompt";
    await fireInput();
    """, preload={})
    assert got["storage"] == {"bridge.compose.compose-1": "half-written prompt"}


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_on_enter_an_empty_compose_field_is_repopulated_from_its_draft_and_enables_the_button(
    tmp_path,
):
    got = _run_compose_draft(tmp_path, """
    window.bridgePage.enter();
    """, preload={"bridge.compose.compose-1": "resume this session"})
    assert got["fieldValue"] == "resume this session"
    assert got["buttonDisabled"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_emptying_the_compose_field_removes_its_draft(tmp_path):
    got = _run_compose_draft(tmp_path, """
    composeField.value = "will be deleted";
    await fireInput();
    composeField.value = "";
    await fireInput();
    """, preload={})
    assert got["storage"] == {}


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_storage_throwing_on_input_is_a_silent_no_op(tmp_path):
    """A blocked or full sessionStorage must not break the delegated input
    handler that also toggles the launch button."""
    got = _run_compose_draft(tmp_path, """
    composeField.value = "typed anyway";
    await fireInput();
    """, preload={}, throw=True)
    assert got["fieldValue"] == "typed anyway"
    assert got["buttonDisabled"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_storage_throwing_on_enter_is_a_silent_no_op(tmp_path):
    """A blocked sessionStorage must not break `onEnter` restoration either --
    the field simply stays whatever the server rendered it as."""
    got = _run_compose_draft(tmp_path, """
    window.bridgePage.enter();
    """, preload={}, throw=True)
    assert got["fieldValue"] == ""


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_restore_never_clobbers_text_the_user_is_already_mid_typing(tmp_path):
    """`restoreComposeDrafts` only restores into a field that is CURRENTLY
    empty (`if (field.value !== "") return;`). Every other restore test here
    starts from an empty field, so that guard has no negative-branch coverage
    -- deleting it would leave every other test green while a stale draft
    silently overwrote in-progress typing on a swap. This sets the field to a
    non-blank value the user has already typed, preloads a DIFFERENT draft
    under the same key, fires `onEnter`, and asserts the user's typed value
    survives untouched."""
    got = _run_compose_draft(tmp_path, """
    composeField.value = "the user is still typing this";
    window.bridgePage.enter();
    """, preload={"bridge.compose.compose-1": "a stale draft from before"})
    assert got["fieldValue"] == "the user is still typing this"


# --- Task 11: the update banner ----------------------------------------------
#
# `update.js` polls `GET /api/diagnostics` once at load (setInterval is
# stubbed to a no-op here, since only ONE tick's worth of behaviour is under
# test) and shows `#update-banner` only when `update.state === "behind"`.
# Dismissal is stored in `localStorage` keyed by the OFFERED sha, and the
# "Update now" button POSTs the exact sha to `/api/update` with the per-install
# bearer token. This proves the LOGIC (which sha is dismissed, what the POST
# carries, whether the banner shows/hides) rather than pixels -- the DOM shim
# has no layout to inspect anyway.
#
# Two `setImmediate` ticks do the waiting: the first lets the initial
# `fetch("/api/diagnostics")` microtask chain (stubbed to a resolved promise,
# so no real I/O delay) settle and `render()` run; the second lets whatever
# the harness fires inside the first tick (a dismiss or apply click, each its
# own promise chain) settle before the result is printed. Node flushes every
# pending microtask before the next macrotask, so nesting `setImmediate` this
# way is sufficient without any real timers.

UPDATE_JS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "update.js"

UPDATE_HARNESS = """
globalThis.window = globalThis;

const storage = Object.assign({}, PRELOAD);
globalThis.localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null),
  setItem: (k, v) => { storage[k] = String(v); },
  removeItem: (k) => { delete storage[k]; },
};

let dismissHandler = null;
let applyHandler = null;

const statusEl = { textContent: "" };
const banner = {
  hidden: true,
  dataset: {},
  querySelector(sel) { return sel === "[data-update-status]" ? statusEl : null; },
};
const applyButton = {
  disabled: false,
  attrs: {},
  addEventListener(type, fn) { if (type === "click") applyHandler = fn; },
  setAttribute(n, v) { this.attrs[n] = v; },
  removeAttribute(n) { delete this.attrs[n]; },
};
const dismissButton = {
  addEventListener(type, fn) { if (type === "click") dismissHandler = fn; },
};
const fromEl = { textContent: "" };
const toEl = { textContent: "" };
const tokenMeta = { content: TOKEN };

let copyHandler = null;
const copyButton = {
  addEventListener(type, fn) { if (type === "click") copyHandler = fn; },
};
const copyStatusEl = { textContent: "" };
const cmdEl = { textContent: "bridge update" };
const bridgeCopyCalls = [];
window.bridgeCopy = (text) => {
  bridgeCopyCalls.push(text);
  return Promise.resolve("✓ Copied to clipboard");
};

const els = {
  "update-banner": banner,
  "update-banner__apply": applyButton,
  "update-banner__dismiss": dismissButton,
  "update-banner__from": fromEl,
  "update-banner__to": toEl,
  "update-banner__copy": copyButton,
  "update-banner__copy-status": copyStatusEl,
  "update-banner__cmd": cmdEl,
};
globalThis.document = {
  getElementById: (id) => els[id] ?? null,
  querySelector: (sel) => (sel === 'meta[name="bridge-update-token"]' ? tokenMeta : null),
};

globalThis.confirm = () => CONFIRM_RESULT;
window.confirm = globalThis.confirm;

const updateCalls = [];
globalThis.fetch = (url, init) => {
  if (url === "/api/diagnostics") {
    return Promise.resolve({ ok: true, json: async () => ({ update: DIAG_UPDATE }) });
  }
  if (url === "/api/update") {
    updateCalls.push({ url, headers: init.headers, body: JSON.parse(init.body) });
    return Promise.resolve(POST_RESPONSE);
  }
  return Promise.reject(new Error("unexpected fetch: " + url));
};

globalThis.setInterval = () => 0;
console.error = () => {};

const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));

setImmediate(() => {
  const afterPoll = { hidden: banner.hidden, from: fromEl.textContent, to: toEl.textContent };

  if (DISMISS) dismissHandler({});
  const afterDismiss = { hidden: banner.hidden };

  if (APPLY) applyHandler({});
  if (COPY) copyHandler({});

  setImmediate(() => {
    console.log(JSON.stringify({
      afterPoll,
      afterDismiss,
      updateCalls,
      status: statusEl.textContent,
      hiddenAfterApply: banner.hidden,
      applyDisabled: applyButton.disabled,
      bridgeCopyCalls,
      copyStatus: copyStatusEl.textContent,
    }));
  });
});
"""


def _run_update_banner(
    tmp_path,
    *,
    diag_update,
    preload=None,
    token="install-token-123",
    confirm_result=True,
    dismiss=False,
    apply=False,
    copy=False,
    post_http_ok=True,
    post_body=None,
    post_status=None,
):
    if post_body is None:
        post_body = {"ok": True}
    if post_status is None:
        post_status = 200 if post_http_ok else 409
    post_response_js = "{ ok: %s, status: %d, json: async () => (%s) }" % (
        "true" if post_http_ok else "false",
        post_status,
        json.dumps(post_body),
    )
    script = (
        UPDATE_HARNESS
        .replace("DIAG_UPDATE", json.dumps(diag_update))
        .replace("PRELOAD", json.dumps(preload or {}))
        .replace("TOKEN", json.dumps(token))
        .replace("CONFIRM_RESULT", "true" if confirm_result else "false")
        .replace("DISMISS", "true" if dismiss else "false")
        .replace("APPLY", "true" if apply else "false")
        .replace("COPY", "true" if copy else "false")
        .replace("POST_RESPONSE", post_response_js)
    )
    harness = tmp_path / "update_harness.js"
    harness.write_text(script)
    proc = subprocess.run([_node(), str(harness), str(UPDATE_JS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


SHA_A = "1111111111111111111111111111111111111111"
SHA_B = "2222222222222222222222222222222222222222"
INSTALLED = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_update_js_keys_dismissal_by_sha():
    """Static companion to the behavioural tests below -- keeps the literal
    tokens the brief calls out grep-able even if the harness logic changes."""
    js = UPDATE_JS.read_text()
    assert "bridge:update-dismissed:" in js
    assert "latest_sha" in js
    assert "/api/update" in js
    assert "Authorization" in js and "Bearer" in js


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_banner_stays_hidden_when_there_is_nothing_to_offer(tmp_path):
    got = _run_update_banner(tmp_path, diag_update=None)
    assert got["afterPoll"]["hidden"] is True

    got = _run_update_banner(
        tmp_path,
        diag_update={"state": "current", "installed_sha": INSTALLED,
                     "latest_sha": None, "checked_at": None, "error": None},
    )
    assert got["afterPoll"]["hidden"] is True


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_banner_shows_the_truncated_shas_only_when_state_is_behind(tmp_path):
    got = _run_update_banner(
        tmp_path,
        diag_update={"state": "behind", "installed_sha": INSTALLED,
                     "latest_sha": SHA_A, "checked_at": "now", "error": None},
    )
    assert got["afterPoll"]["hidden"] is False
    assert got["afterPoll"]["from"] == INSTALLED[:12]
    assert got["afterPoll"]["to"] == SHA_A[:12]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_dismissing_one_sha_hides_the_banner_for_that_offer(tmp_path):
    got = _run_update_banner(
        tmp_path,
        diag_update={"state": "behind", "installed_sha": INSTALLED,
                     "latest_sha": SHA_A, "checked_at": "now", "error": None},
        dismiss=True,
    )
    assert got["afterPoll"]["hidden"] is False, "banner must be visible before it can be dismissed"
    assert got["afterDismiss"]["hidden"] is True


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_dismissal_recorded_for_sha_a_never_suppresses_sha_b(tmp_path):
    """The exact regression this task exists to prevent: a stale
    `bridge:update-dismissed:<sha>` entry from a PRIOR offer must not silence
    a later, different commit the checker now reports as `behind`."""
    dismissed_key = "bridge:update-dismissed:" + SHA_A

    # A fresh load that still offers the SAME sha A stays suppressed...
    got_same = _run_update_banner(
        tmp_path,
        diag_update={"state": "behind", "installed_sha": INSTALLED,
                     "latest_sha": SHA_A, "checked_at": "now", "error": None},
        preload={dismissed_key: "1"},
    )
    assert got_same["afterPoll"]["hidden"] is True

    # ...but a fresh load offering a DIFFERENT sha B must show, even though
    # sha A's dismissal is still sitting in localStorage.
    got_other = _run_update_banner(
        tmp_path,
        diag_update={"state": "behind", "installed_sha": INSTALLED,
                     "latest_sha": SHA_B, "checked_at": "now", "error": None},
        preload={dismissed_key: "1"},
    )
    assert got_other["afterPoll"]["hidden"] is False
    assert got_other["afterPoll"]["to"] == SHA_B[:12]


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_update_now_posts_the_offered_sha_with_the_bearer_token(tmp_path):
    got = _run_update_banner(
        tmp_path,
        diag_update={"state": "behind", "installed_sha": INSTALLED,
                     "latest_sha": SHA_A, "checked_at": "now", "error": None},
        token="the-install-token",
        apply=True,
    )
    assert len(got["updateCalls"]) == 1
    call = got["updateCalls"][0]
    assert call["url"] == "/api/update"
    assert call["headers"]["Authorization"] == "Bearer the-install-token"
    assert call["body"] == {"target_sha": SHA_A}


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_copy_fallback_uses_the_shared_bridge_copy_helper_not_a_new_clipboard_path(tmp_path):
    """The `bridge update` command is copy-able independently of whether the
    one-click button ever works -- and reuses `window.bridgeCopy` (copy.js)
    rather than a second clipboard implementation, so it gets the exact same
    select-and-tell fallback "Copy prompt" already relies on."""
    got = _run_update_banner(
        tmp_path,
        diag_update={"state": "behind", "installed_sha": INSTALLED,
                     "latest_sha": SHA_A, "checked_at": "now", "error": None},
        copy=True,
    )
    assert got["bridgeCopyCalls"] == ["bridge update"]
    assert got["copyStatus"] == "✓ Copied to clipboard"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_declining_the_confirmation_sends_no_request(tmp_path):
    got = _run_update_banner(
        tmp_path,
        diag_update={"state": "behind", "installed_sha": INSTALLED,
                     "latest_sha": SHA_A, "checked_at": "now", "error": None},
        apply=True,
        confirm_result=False,
    )
    assert got["updateCalls"] == []


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_failed_update_stays_on_screen_and_retryable_with_its_error_visible(tmp_path):
    """A failed apply must not dismiss itself -- the button stays usable for a
    retry, and the failure (plus the `bridge update` fallback) is on screen
    rather than in a transient alert only the clicking user ever saw."""
    got = _run_update_banner(
        tmp_path,
        diag_update={"state": "behind", "installed_sha": INSTALLED,
                     "latest_sha": SHA_A, "checked_at": "now", "error": None},
        apply=True,
        post_http_ok=True,
        post_body={"ok": False, "error": "git fetch failed"},
    )
    assert got["hiddenAfterApply"] is False, "a failed update must stay retryable, not hide itself"
    assert "git fetch failed" in got["status"]
    assert "bridge update" in got["status"]
    assert got["applyDisabled"] is False, "the button must re-enable so the user can retry"


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_a_successful_update_announces_success_without_hiding_the_banner(tmp_path):
    """This synchronous-success branch is the UNMANAGED (`bridge serve`) path:
    the install landed but this in-process panel does NOT auto-restart, so the
    copy must tell the user to restart it -- not the false "the panel will
    restart shortly". The banner stays visible carrying that instruction rather
    than silently vanishing."""
    got = _run_update_banner(
        tmp_path,
        diag_update={"state": "behind", "installed_sha": INSTALLED,
                     "latest_sha": SHA_A, "checked_at": "now", "error": None},
        apply=True,
        post_http_ok=True,
        post_body={"ok": True},
    )
    assert "Update installed" in got["status"]
    assert "restart the panel" in got["status"]
    assert "restart shortly" not in got["status"]
    assert got["hiddenAfterApply"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_an_accepted_async_response_shows_updating_and_keeps_the_button_disabled(tmp_path):
    """The managed-LaunchAgent path answers 202 `{accepted: true}`: a detached
    one-shot job installs and RESTARTS the panel, so the SSE stream drops and
    this page reconnects. There is no synchronous result to mark success or
    failure -- so the banner shows an "updating…/reconnect" state and the button
    stays disabled, and it must NOT be mistaken for a failure (the old code fell
    through to the error branch on any response lacking `data.ok`)."""
    got = _run_update_banner(
        tmp_path,
        diag_update={"state": "behind", "installed_sha": INSTALLED,
                     "latest_sha": SHA_A, "checked_at": "now", "error": None},
        apply=True,
        post_http_ok=True,
        post_status=202,
        post_body={"accepted": True, "target_sha": SHA_A},
    )
    assert "reconnect" in got["status"].lower()
    assert "⚠" not in got["status"], "an accepted async update must not read as a failure"
    assert got["applyDisabled"] is True, "the panel is restarting; the button stays disabled"
    assert got["hiddenAfterApply"] is False


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_an_accepted_body_without_a_202_status_is_still_treated_as_async(tmp_path):
    """Detection keys on EITHER the 202 status OR an `{accepted: true}` body, so
    a 200-carrying accepted shape is still handled as the async path, never as a
    synchronous success/failure."""
    got = _run_update_banner(
        tmp_path,
        diag_update={"state": "behind", "installed_sha": INSTALLED,
                     "latest_sha": SHA_A, "checked_at": "now", "error": None},
        apply=True,
        post_http_ok=True,
        post_status=200,
        post_body={"accepted": True},
    )
    assert "reconnect" in got["status"].lower()
    assert got["applyDisabled"] is True
