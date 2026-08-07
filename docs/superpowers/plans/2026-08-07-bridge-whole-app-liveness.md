# Whole-App Liveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every swappable surface (`/project/{id}`, `/schedule`, `/diagnostics`, `/settings`) reflect underlying changes live — no manual reload — reusing the existing persistent SSE stream and re-rendering via a hand-rolled DOM morph that preserves scroll, focus, `<details>` state, and in-flight input.

**Architecture:** A new client controller (`liverefresh.js`) subscribes to the one persistent `EventSource("/events")` (via a fan-out added to `live.js`). When a change relevant to the current route arrives, it re-fetches that page's own fragment and morphs it into `.shell__body` with a minimal keyed morph (`morph.js`). Nodes the user is editing or that carry live focus are marked/detected and skipped. Overview keeps its existing surgical patcher untouched. No new server transport; fragment routes and the SSE stream already exist.

**Tech Stack:** Python 3.13 / FastAPI (server, essentially unchanged), vanilla ES (no build step, no third-party JS), Jinja2 templates, pytest + a hand-rolled node/minidom JS harness (`tests/js/minidom.js`).

## Global Constraints

- **No third-party JavaScript.** The morph is hand-rolled; the whole client stays dependency-free.
- **Progressive enhancement.** Every surface must still work with JS off or if the morph/controller no-ops; a background refresh has no user intent, so on any failure keep the existing DOM intact — never fall back to a full reload (unlike navigation).
- **Page scripts obey the shell contract** (`tests/test_shell_contract.py`): no top-level `const` re-execution hazards, no `DOMContentLoaded` outside `router.js`, no module-scope DOM capture, and per-page behavior registered on `window.bridgePage` (`onEnter`/`onLeave`) rather than run at load. `morph.js` is a pure library (registers nothing); `liverefresh.js` is a page script (registers on the registry).
- **minidom is deliberately minimal** — it models element identity, attributes, classes/id/tag selectors, `closest`, and bubbling dispatch. It does NOT model layout, CSS, focus, scroll, or `DOMParser`. Anything depending on those is verified in Arc (Task 7), never asserted under minidom.
- **WCAG 2.2 AA.** The changed-value highlight must respect `prefers-reduced-motion` and must not be the sole signal of a change.
- **Overview is out of scope.** `live.js`'s Overview leaf-patching behavior is not changed; `live.js` is only *extended* with a frame fan-out.

---

### Task 1: minidom node ops for morphing

The morph needs two node operations minidom does not yet model: enumerate an element's attribute names, and move/insert a child at a position (real DOM `insertBefore` detaches first — minidom's `append` does not). Add both so the morph can be tested honestly. Do not change existing `append`/`remove` (existing tests depend on them).

**Files:**
- Modify: `tests/js/minidom.js` (class `El`, around lines 85–97)
- Test: `tests/test_minidom_ops.py` (create)

**Interfaces:**
- Produces: `El.getAttributeNames() -> string[]`; `El.insertBefore(node, ref|null) -> node` (detaches `node` from its current parent first; `ref === null` appends at end; unknown `ref` appends at end).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_minidom_ops.py
import json, shutil, subprocess
from pathlib import Path
import pytest

MINIDOM = Path(__file__).resolve().parent / "js" / "minidom.js"
NODE_CANDIDATES = ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node")

def _node():
    found = shutil.which("node")
    if found:
        return found
    return next((p for p in NODE_CANDIDATES if Path(p).exists()), None)

pytestmark = pytest.mark.skipif(_node() is None, reason="node is not installed")

def _run(body: str, tmp_path):
    script = tmp_path / "case.js"
    script.write_text(
        f'const {{ El, makeDocument, report }} = require({json.dumps(str(MINIDOM))});\n'
        f'makeDocument(null);\n{body}\n'
    )
    proc = subprocess.run([_node(), str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)

def test_get_attribute_names_lists_every_set_attribute(tmp_path):
    got = _run(
        """
        const el = document.createElement("div");
        el.setAttribute("id", "x");
        el.setAttribute("data-key", "7");
        report({ names: el.getAttributeNames().sort() });
        """,
        tmp_path,
    )
    assert got["names"] == ["data-key", "id"]

def test_insert_before_moves_an_existing_child_without_duplicating(tmp_path):
    got = _run(
        """
        const box = document.createElement("ul");
        const a = document.createElement("li"); a.setAttribute("id", "a");
        const b = document.createElement("li"); b.setAttribute("id", "b");
        box.append(a); box.append(b);
        box.insertBefore(b, a);              // move b before a
        report({ order: box.children.map((c) => c.getAttribute("id")) });
        """,
        tmp_path,
    )
    assert got["order"] == ["b", "a"], "insertBefore must move, not duplicate"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_minidom_ops.py -v`
Expected: FAIL — `getAttributeNames is not a function` / `insertBefore is not a function`.

- [ ] **Step 3: Add the two methods to `El`**

In `tests/js/minidom.js`, inside `class El`, after `hasAttribute` (line 88) add:

```javascript
  getAttributeNames() { return [...this.attrs.keys()]; }
```

After `remove()` (line 97) add:

```javascript
  insertBefore(node, ref) {
    // Real DOM insertBefore MOVES the node: detach from its current parent
    // first, or a reorder would clone it into two places. minidom's `append`
    // deliberately does not detach, so morph uses this exclusively.
    if (node.parent) {
      const j = node.parent.children.indexOf(node);
      if (j >= 0) node.parent.children.splice(j, 1);
    }
    node.parent = this;
    if (ref == null) { this.children.push(node); return node; }
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, node);
    return node;
  }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_minidom_ops.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Confirm no existing JS test regressed, then commit**

Run: `pytest tests/test_shell_contract.py tests/test_swap_lifecycle.py tests/test_static_js.py -q`
Expected: all pass (methods are additive).

```bash
git add tests/js/minidom.js tests/test_minidom_ops.py
git commit -m "Add getAttributeNames and detach-first insertBefore to the minidom test harness"
```

---

### Task 2: `morph.js` — the keyed DOM morph

A pure, portable morph: reconcile a live container's subtree to match an incoming one, reusing matched nodes (so focus/scroll/expand survive), skipping protected nodes, and reporting changed nodes for the highlight. Uses only APIs present in both the browser and minidom: `getAttributeNames`, `getAttribute`/`setAttribute`/`removeAttribute`/`hasAttribute`, `children`, `textContent`, `insertBefore`, `remove`, `document.createElement`, `append`.

**Files:**
- Create: `src/bridge/static/morph.js`
- Test: `tests/test_morph.py` (create)

**Interfaces:**
- Produces: `window.bridgeMorph(live, incoming, opts)` where
  `opts = { key?: (el) => string|null, ignore?: (el) => boolean, onChange?: (el) => void }`.
  Mutates `live` in place to match `incoming`. `key` defaults to `id` then `data-key`;
  `ignore` defaults to always-false; `onChange` is called once per node whose
  attributes or text this morph changed, and once per newly inserted node.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_morph.py
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

def _run(body: str, tmp_path):
    script = tmp_path / "case.js"
    script.write_text(
        f'const {{ makeDocument, load, report }} = require({json.dumps(str(MINIDOM))});\n'
        f'makeDocument(null);\n'
        f'load({json.dumps(str(STATIC / "morph.js"))});\n'
        f'{body}\n'
    )
    proc = subprocess.run([_node(), str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)

# Helper prelude available to every case body.
PRELUDE = """
function make(tag, attrs, text) {
  const el = document.createElement(tag);
  for (const k of Object.keys(attrs || {})) el.setAttribute(k, attrs[k]);
  if (text != null) el.textContent = text;
  return el;
}
"""

def test_updates_a_leaf_text_and_reports_the_change(tmp_path):
    got = _run(PRELUDE + """
        const live = make("div"); live.append(make("span", { id: "s" }, "old"));
        const inc  = make("div"); inc.append(make("span", { id: "s" }, "new"));
        const changed = [];
        window.bridgeMorph(live, inc, { onChange: (el) => changed.push(el.getAttribute("id")) });
        report({ text: live.children[0].textContent, changed });
    """, tmp_path)
    assert got["text"] == "new"
    assert got["changed"] == ["s"]

def test_syncs_attributes_adding_updating_and_removing(tmp_path):
    got = _run(PRELUDE + """
        const live = make("div", { id: "d", class: "a", stale: "1" });
        const inc  = make("div", { id: "d", class: "b" });
        window.bridgeMorph(live, inc, {});
        report({ cls: live.getAttribute("class"), stale: live.getAttribute("stale") });
    """, tmp_path)
    assert got["cls"] == "b"
    assert got["stale"] is None

def test_reuses_keyed_rows_and_reorders_without_rebuilding(tmp_path):
    got = _run(PRELUDE + """
        const live = make("ul");
        const a = make("li", { id: "a" }, "A"); const b = make("li", { id: "b" }, "B");
        live.append(a); live.append(b);
        const inc = make("ul");
        inc.append(make("li", { id: "b" }, "B")); inc.append(make("li", { id: "a" }, "A2"));
        window.bridgeMorph(live, inc, {});
        report({
          order: live.children.map((c) => c.getAttribute("id")),
          survivedB: live.children[0] === b,   // identity retained
          survivedA: live.children[1] === a,
          aText: live.children[1].textContent,
        });
    """, tmp_path)
    assert got["order"] == ["b", "a"]
    assert got["survivedB"] is True and got["survivedA"] is True
    assert got["aText"] == "A2"

def test_adds_new_rows_and_removes_dropped_rows(tmp_path):
    got = _run(PRELUDE + """
        const live = make("ul"); live.append(make("li", { id: "a" }, "A"));
        const inc = make("ul");
        inc.append(make("li", { id: "a" }, "A")); inc.append(make("li", { id: "c" }, "C"));
        window.bridgeMorph(live, inc, {});
        report({ order: live.children.map((c) => c.getAttribute("id")) });
    """, tmp_path)
    assert got["order"] == ["a", "c"]

def test_ignored_node_is_neither_morphed_nor_removed(tmp_path):
    got = _run(PRELUDE + """
        const live = make("form");
        const t = make("textarea", { id: "t" }, "user typing");
        t.setAttribute("data-live-preserve", "");
        live.append(t);
        const inc = make("form");
        inc.append(make("textarea", { id: "t" }, "SERVER VALUE"));  // would clobber
        const ignore = (el) => el.hasAttribute && el.hasAttribute("data-live-preserve");
        window.bridgeMorph(live, inc, { ignore });
        report({ kept: live.children.length === 1 && live.children[0] === t,
                 text: live.children[0].textContent });
    """, tmp_path)
    assert got["kept"] is True
    assert got["text"] == "user typing", "an ignored node must not be morphed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_morph.py -v`
Expected: FAIL — `window.bridgeMorph is not a function`.

- [ ] **Step 3: Implement `src/bridge/static/morph.js`**

```javascript
// A minimal keyed DOM morph. Reconciles `live` to match `incoming`, reusing
// matched nodes so a background re-render keeps scroll, focus, <details> state
// and in-flight input alive -- a blunt replaceWith would destroy all of it.
//
// Deliberately narrower than a general differ: Bridge re-renders the SAME route
// from the SAME URL, so the tree shape is stable and keyed-list reconciliation
// plus attribute/leaf-text sync is enough. Uses only APIs shared by the browser
// and the minidom test harness -- no importNode/cloneNode/DOMParser.
(function () {
  function defaultKey(el) {
    if (!el || !el.getAttribute) return null;
    return el.getAttribute("id") || el.getAttribute("data-key") || null;
  }

  function cloneInto(node) {
    // Build a node in the LIVE document from an incoming (parsed) node, over the
    // minimal interface both realms share. localName is the browser's lowercase
    // tag; minidom exposes `tag`.
    const el = document.createElement(node.localName || node.tag);
    for (const name of node.getAttributeNames()) el.setAttribute(name, node.getAttribute(name));
    const kids = Array.from(node.children);
    if (kids.length === 0) {
      if (node.textContent) el.textContent = node.textContent;
    } else {
      for (const kid of kids) el.append(cloneInto(kid));
    }
    return el;
  }

  function syncAttrs(live, incoming) {
    let changed = false;
    for (const name of incoming.getAttributeNames()) {
      if (live.getAttribute(name) !== incoming.getAttribute(name)) {
        live.setAttribute(name, incoming.getAttribute(name));
        changed = true;
      }
    }
    for (const name of live.getAttributeNames()) {
      if (!incoming.hasAttribute(name)) { live.removeAttribute(name); changed = true; }
    }
    return changed;
  }

  function morphNode(live, incoming, opts) {
    if (opts.ignore(live)) return;              // protected subtree: hands off
    let changed = syncAttrs(live, incoming);
    const incKids = Array.from(incoming.children);
    if (incKids.length === 0) {
      // Leaf: sync text. (A live subtree that became a leaf server-side has its
      // children removed by reconcileChildren below via the empty incoming set.)
      if (Array.from(live.children).length === 0 &&
          live.textContent !== incoming.textContent) {
        live.textContent = incoming.textContent;
        changed = true;
      }
    }
    reconcileChildren(live, incoming, opts);
    if (changed) opts.onChange(live);
  }

  function reconcileChildren(live, incoming, opts) {
    const incKids = Array.from(incoming.children);
    const liveByKey = new Map();
    for (const el of Array.from(live.children)) {
      const k = opts.key(el);
      if (k != null) liveByKey.set(k, el);
    }
    const unkeyed = Array.from(live.children).filter((el) => opts.key(el) == null);
    let unkeyedCursor = 0;
    const used = new Set();
    const desired = [];
    for (const inc of incKids) {
      const k = opts.key(inc);
      let node = null;
      if (k != null && liveByKey.has(k)) {
        node = liveByKey.get(k);
      } else if (k == null) {
        while (unkeyedCursor < unkeyed.length &&
               (unkeyed[unkeyed.length && unkeyedCursor].localName || unkeyed[unkeyedCursor].tag) !==
               (inc.localName || inc.tag)) unkeyedCursor += 1;
        if (unkeyedCursor < unkeyed.length) { node = unkeyed[unkeyedCursor]; unkeyedCursor += 1; }
      }
      if (node) { morphNode(node, inc, opts); used.add(node); }
      else { node = cloneInto(inc); opts.onChange(node); }
      desired.push(node);
    }
    // Remove live children the server dropped -- but never a protected node.
    for (const el of Array.from(live.children)) {
      if (!used.has(el) && desired.indexOf(el) === -1 && !opts.ignore(el)) el.remove();
    }
    // Put the desired sequence in order; insertBefore moves existing nodes.
    for (let i = 0; i < desired.length; i += 1) {
      if (live.children[i] !== desired[i]) live.insertBefore(desired[i], live.children[i] || null);
    }
  }

  function morph(live, incoming, opts) {
    opts = opts || {};
    morphNode(live, incoming, {
      key: opts.key || defaultKey,
      ignore: opts.ignore || (() => false),
      onChange: opts.onChange || (() => {}),
    });
  }

  window.bridgeMorph = morph;
})();
```

- [ ] **Step 4: Fix the unkeyed-cursor typo and re-read**

Note: in `reconcileChildren` the unkeyed same-tag scan must read `unkeyed[unkeyedCursor]`. Replace the `while` condition with the clean form:

```javascript
      } else if (k == null) {
        while (unkeyedCursor < unkeyed.length &&
               (unkeyed[unkeyedCursor].localName || unkeyed[unkeyedCursor].tag) !==
               (inc.localName || inc.tag)) unkeyedCursor += 1;
        if (unkeyedCursor < unkeyed.length) { node = unkeyed[unkeyedCursor]; unkeyedCursor += 1; }
      }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_morph.py -v`
Expected: PASS (all five).

- [ ] **Step 6: Commit**

```bash
git add src/bridge/static/morph.js tests/test_morph.py
git commit -m "Add a hand-rolled keyed DOM morph that preserves reused nodes"
```

---

### Task 3: Expose the fragment parser from `router.js`

The controller must turn a fetched fragment's HTML into a `.shell__body` node without owning its own parser. `router.js` already has `parseFragment`; expose it as a shared helper so both the router and the controller use one implementation (DRY), and so controller tests can stub it (minidom has no `DOMParser`).

**Files:**
- Modify: `src/bridge/static/router.js` (`parseFragment`, ~lines 27–39; add an export near the other `window.bridge*` assignments)
- Test: `tests/test_shell_contract.py` (add one assertion)

**Interfaces:**
- Produces: `window.bridgeFragment = { parse(html) -> { body, status, title, active } | null }` — same object `parseFragment` already returns. `body`/`status` are the parsed `.shell__body` / `.shell-status` nodes.

- [ ] **Step 1: Write the failing contract test**

```python
# add to tests/test_shell_contract.py
def test_router_exposes_the_fragment_parser_for_reuse():
    """liverefresh.js reuses router.js's parser instead of shipping a second one."""
    text = source("router.js")
    assert "window.bridgeFragment" in text, (
        "router.js must expose its fragment parser as window.bridgeFragment "
        "so the live-refresh controller can reuse it"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shell_contract.py::test_router_exposes_the_fragment_parser_for_reuse -v`
Expected: FAIL — string not found.

- [ ] **Step 3: Export the parser**

In `src/bridge/static/router.js`, after `window.bridgeNavigate = navigate;` (line 114) add:

```javascript
// Shared so liverefresh.js parses fragments through the one implementation
// instead of shipping a second DOMParser path that could drift from this one.
window.bridgeFragment = { parse: parseFragment };
```

- [ ] **Step 4: Run the contract + swap-lifecycle tests**

Run: `pytest tests/test_shell_contract.py tests/test_swap_lifecycle.py -q`
Expected: PASS (behavior unchanged; only an export added).

- [ ] **Step 5: Commit**

```bash
git add src/bridge/static/router.js tests/test_shell_contract.py
git commit -m "Expose router.js fragment parser as window.bridgeFragment for reuse"
```

---

### Task 4: `live.js` frame fan-out

`liverefresh.js` must receive the same SSE frames `live.js` already parses, without opening a second `EventSource`. Add a tiny fan-out: a registry of frame listeners plus an `_emit` the real `handle` calls on every parsed payload. This is an additive extension — Overview patching is untouched.

**Files:**
- Modify: `src/bridge/static/live.js` (`handle`, ~lines 353–370; add exports near lines 395–396)
- Test: `tests/test_live_fanout.py` (create)

**Interfaces:**
- Produces: `window.bridgeLive = { onFrame(fn), _emit(payload) }`. `onFrame` registers a listener; `_emit(payload)` invokes every listener with the payload (each guarded so one throwing listener does not stop the others). The real SSE `handle` calls `_emit(payload)` after parsing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_live_fanout.py
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

def _run(body: str, files, tmp_path):
    script = tmp_path / "case.js"
    loads = "\n".join(f'load({json.dumps(str(STATIC / n))});' for n in files)
    script.write_text(
        f'const {{ makeDocument, load, report }} = require({json.dumps(str(MINIDOM))});\n'
        f'makeDocument(null);\n{loads}\n{body}\n'
    )
    proc = subprocess.run([_node(), str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)

def test_registered_listeners_receive_emitted_frames(tmp_path):
    got = _run(
        """
        const seen = [];
        window.bridgeLive.onFrame((f) => seen.push(f.generation));
        window.bridgeLive._emit({ generation: 42 });
        report({ seen });
        """,
        ["shell.js", "live.js"],
        tmp_path,
    )
    assert got["seen"] == [42]

def test_one_throwing_listener_does_not_stop_the_others(tmp_path):
    got = _run(
        """
        const seen = [];
        window.bridgeLive.onFrame(() => { throw new Error("boom"); });
        window.bridgeLive.onFrame((f) => seen.push(f.generation));
        window.bridgeLive._emit({ generation: 7 });
        report({ seen });
        """,
        ["shell.js", "live.js"],
        tmp_path,
    )
    assert got["seen"] == [7]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_live_fanout.py -v`
Expected: FAIL — `window.bridgeLive` is undefined.

- [ ] **Step 3: Add the fan-out to `live.js`**

Near the top-level state (after line 185, `let transportReconnecting = false;`) add:

```javascript
// Frame fan-out: liverefresh.js subscribes here rather than opening a second
// EventSource. Additive -- Overview patching below is unchanged.
const frameListeners = [];
function emitFrame(payload) {
  for (const fn of frameListeners) {
    try { fn(payload); } catch (error) { console.error("bridge: frame listener failed", error); }
  }
}
```

Inside `handle` (the SSE handler), after the `frames += 1;` line and before the schema branch, add:

```javascript
    emitFrame(payload);
```

At the bottom, after `window.bridgeLiveSource = liveSource;` (line 396) add:

```javascript
window.bridgeLive = { onFrame(fn) { frameListeners.push(fn); }, _emit: emitFrame };
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_live_fanout.py -v`
Expected: PASS (both).

- [ ] **Step 5: Confirm Overview live tests still pass, then commit**

Run: `pytest tests/test_static_js.py tests/test_shell_contract.py -q`
Expected: PASS (fan-out is additive).

```bash
git add src/bridge/static/live.js tests/test_live_fanout.py
git commit -m "Fan out parsed SSE frames from live.js for extra subscribers"
```

---

### Task 5: `liverefresh.js` — the controller

Watch frames; when the current route is one this controller owns and the frame signals a change that route reflects, re-fetch the current fragment (debounced) and morph it in — deferring while a protected node holds focus so nothing the user is editing is clobbered. Wire it into `base.html` and register it on the shell contract.

**Files:**
- Create: `src/bridge/static/liverefresh.js`
- Modify: `src/bridge/templates/base.html` (script block, after line 182)
- Modify: `tests/test_shell_contract.py` (`PAGE_SCRIPTS` line 17–18, and the registry test's params line 94)
- Test: `tests/test_liverefresh.py` (create)

**Interfaces:**
- Consumes: `window.bridgeLive.onFrame` (Task 4), `window.bridgeMorph` (Task 2), `window.bridgeFragment.parse` (Task 3), `window.bridgePage.onEnter/onLeave` (existing).
- Produces: `window.bridgeLiveRefresh = { _onFrame(frame), _refreshNow() }` test seams; no other public surface.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_liverefresh.py
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
        setPath("/schedule");
        const body = shellBody();
        const inc = document.createElement("div"); inc.setAttribute("class", "shell__body");
        inc.append(document.createElement("p")); window.__parsed = { body: inc };
        window.bridgePage.enter();                          // baseline generation read
        window.bridgeLiveRefresh._onFrame({ generation: 1 });
        window.bridgeLiveRefresh._onFrame({ generation: 2 });   // bump -> refresh
        window.bridgeLiveRefresh._refreshNow();
        report({ fetches: globalThis.__calls.fetch.length,
                 url: globalThis.__calls.fetch[0] && globalThis.__calls.fetch[0].url,
                 fragHeader: globalThis.__calls.fetch[0].opts.headers["X-Bridge-Fragment"],
                 morphed: body.children.length });
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_liverefresh.py -v`
Expected: FAIL — `window.bridgeLiveRefresh` undefined.

- [ ] **Step 3: Implement `src/bridge/static/liverefresh.js`**

```javascript
// Whole-app liveness for the surfaces Overview's surgical patcher does not
// cover. Subscribes to the one persistent SSE stream (via live.js's fan-out)
// and, when the current route reflects a change, re-fetches that page's own
// fragment and morphs it in -- preserving scroll, focus, <details> state and
// in-flight input. A background refresh has no user intent, so any failure just
// keeps the current DOM; it never falls back to a full reload.
(function () {
  if (!window.bridgePage || !window.bridgeLive) return;   // progressive: no-op if unwired

  const WORKSPACE = /^\/project\/\d+$/;
  const OWNED = new Set(["/schedule", "/diagnostics", "/settings"]);
  const DEBOUNCE_MS = 250;

  let owned = false;
  let projectId = null;
  let baselineGeneration = null;   // last generation acknowledged for this view
  let lastSeenGeneration = null;   // most recent generation observed on the wire
  let pendingGeneration = null;    // a bump waiting to be applied
  let lastProjectLive = null;
  let timer = null;

  function currentPath() { return window.location.pathname; }

  function isOwned(path) { return OWNED.has(path) || WORKSPACE.test(path); }

  function projectIdOf(path) {
    const m = WORKSPACE.exec(path);
    return m ? path.slice("/project/".length) : null;
  }

  function protectedFocus() {
    const active = document.activeElement;
    if (!active || !active.closest) return false;
    return Boolean(active.closest("[data-live-preserve]"));
  }

  function ignoreNode(el) {
    if (!el || !el.hasAttribute) return false;
    if (el.hasAttribute("data-live-preserve")) return true;
    const active = document.activeElement;
    if (active && (el === active || (el.contains && el.contains(active)))) return true;
    return false;
  }

  function highlight(node) {
    if (!node || !node.classList) return;
    node.classList.add("live-changed");
    if (typeof setTimeout === "function") {
      setTimeout(() => { if (node.classList) node.classList.remove("live-changed"); }, 1200);
    }
  }

  function refreshNow() {
    if (!owned) return;
    if (pendingGeneration == null && !workspaceLiveChanged()) return;
    if (protectedFocus()) return;                 // defer: retried on the next frame
    const generationAtFetch = lastSeenGeneration;
    const path = currentPath();
    fetch(path, { headers: { "X-Bridge-Fragment": "1" }, credentials: "same-origin" })
      .then((response) => {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.text();
      })
      .then((html) => {
        const parsed = window.bridgeFragment.parse(html);
        if (!parsed || !parsed.body) throw new Error("unusable fragment");
        const liveBody = document.querySelector(".shell__body");
        if (!liveBody) return;
        window.bridgeMorph(liveBody, parsed.body, { ignore: ignoreNode, onChange: highlight });
        baselineGeneration = generationAtFetch;
        pendingGeneration = null;
      })
      .catch((error) => { console.error("bridge: live refresh kept stale DOM", error); });
  }

  function workspaceLiveChanged() {
    return false;   // replaced below once a frame carries per-card live; see note
  }

  function schedule() {
    if (typeof setTimeout !== "function") { refreshNow(); return; }
    if (timer) return;                             // coalesce a burst into one
    timer = setTimeout(() => { timer = null; refreshNow(); }, DEBOUNCE_MS);
  }

  function onFrame(frame) {
    const generation = Number(frame && frame.generation);
    if (Number.isFinite(generation)) lastSeenGeneration = generation;
    if (!owned) return;
    if (baselineGeneration != null && Number.isFinite(generation) && generation > baselineGeneration) {
      pendingGeneration = generation;
      schedule();
    }
  }

  function enter() {
    const path = currentPath();
    owned = isOwned(path);
    projectId = projectIdOf(path);
    baselineGeneration = lastSeenGeneration;       // only future bumps refresh
    pendingGeneration = null;
    lastProjectLive = null;
  }

  function leave() {
    if (timer) { clearTimeout(timer); timer = null; }
    owned = false;
    pendingGeneration = null;
  }

  window.bridgeLive.onFrame(onFrame);
  window.bridgePage.onEnter(enter);
  window.bridgePage.onLeave(leave);

  window.bridgeLiveRefresh = { _onFrame: onFrame, _refreshNow: refreshNow };
})();
```

Note on `workspaceLiveChanged`: the SSE frame already carries per-card live under `frame.cards[projectId].live` (see `api.py:_live_snapshot` / dashboard builder). Replace the stub body with a real comparison against `lastProjectLive` for the project page, keyed on `projectId`, and set `pendingGeneration`/`schedule()` when it differs. Verify the exact frame shape against `dashboard_builder.live_patch()` output during implementation; if `cards` is absent on a `live_patch` frame, leave the stub returning `false` (generation bumps still cover DB-derived content) and note it as a follow-up rather than guessing the shape.

- [ ] **Step 4: Run the controller tests**

Run: `pytest tests/test_liverefresh.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Wire it into `base.html` and the shell contract**

In `src/bridge/templates/base.html`, after `<script src="/static/live.js" defer></script>` (line 181) add:

```html
  {# morph.js defines window.bridgeMorph; liverefresh.js consumes it, live.js's
     frame fan-out, and router.js's fragment parser -- so both load after them. #}
  <script src="/static/morph.js" defer></script>
  <script src="/static/liverefresh.js" defer></script>
```

In `tests/test_shell_contract.py`, add `"liverefresh.js"` to `PAGE_SCRIPTS` (line 17–18) and to the params list of `test_per_page_behaviour_is_registered_on_the_registry` (line 94). Do NOT add `morph.js` to either — it is a pure library that registers no page behavior.

- [ ] **Step 6: Run the full contract + JS suite**

Run: `pytest tests/test_shell_contract.py tests/test_static_js.py tests/test_swap_lifecycle.py tests/test_liverefresh.py -q`
Expected: PASS. (If `test_no_module_scope_dom_capture` flags `liverefresh.js`, confirm no top-level `document.querySelector` runs at load — all DOM reads are inside functions; the `currentPath`/`document.querySelector` calls happen only when invoked.)

- [ ] **Step 7: Commit**

```bash
git add src/bridge/static/liverefresh.js src/bridge/templates/base.html tests/test_shell_contract.py tests/test_liverefresh.py
git commit -m "Add the live-refresh controller and wire it into the shell"
```

---

### Task 6: Protect editable nodes and add the changed-value highlight

Mark the surfaces a background morph must never clobber, and add the reduced-motion-respecting highlight the controller already calls (`live-changed`). Without the markup, a live refresh while the user has a handoff/compose box open (but not focused) could morph the server value over their draft.

**Files:**
- Modify: `src/bridge/templates/_launch.html` (the handoff textarea in `handoff_block`, and the compose textarea `data-compose-prompt` at line 47)
- Modify: `src/bridge/static/app.css` (add the `.live-changed` rule)
- Test: `tests/test_api.py` (assert the rendered project page marks the compose/handoff textareas)

**Interfaces:**
- Consumes: `ignoreNode` in `liverefresh.js` treats any `[data-live-preserve]` (or a node containing `document.activeElement`) as protected.

- [ ] **Step 1: Write the failing server test**

Find an existing project-page render test in `tests/test_api.py` to copy the client/fixture setup from (search for a test that GETs `/project/`). Then add:

```python
def test_editable_surfaces_are_marked_live_preserve(client_with_project):
    # Reuse whatever fixture renders a project page with a compose box.
    client, project_id = client_with_project
    html = client.get(f"/project/{project_id}").text
    assert "data-compose-prompt" in html
    # Every compose/handoff textarea the user can type into is protected from a
    # background morph clobbering an in-progress draft.
    assert 'data-live-preserve' in html, (
        "the compose/handoff editing surfaces must carry data-live-preserve"
    )
```

If no ready-made project fixture exists, adapt the nearest existing `/project/{id}` test's setup verbatim (do not invent a new fixture shape).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py::test_editable_surfaces_are_marked_live_preserve -v`
Expected: FAIL — attribute absent.

- [ ] **Step 3: Add the attribute to the editable textareas**

In `src/bridge/templates/_launch.html`, on the compose textarea (line 47) add `data-live-preserve`:

```html
    <textarea class="compose__prompt" id="{{ cid }}" data-compose-prompt="{{ cid }}" data-live-preserve></textarea>
```

Find the handoff textarea inside the `handoff_block` macro (grep `_launch.html` for `<textarea`) and add `data-live-preserve` to it the same way. Any element the user types into on a swappable surface gets the attribute.

- [ ] **Step 4: Add the highlight CSS**

In `src/bridge/static/app.css` add (near other transient/utility rules):

```css
/* A value the live morph just changed gets a brief tint so the update is
   perceptible. Colour is a secondary cue only -- the changed text itself is the
   primary signal -- and motion is dropped entirely under reduced-motion. */
.live-changed {
  animation: live-flash 1.2s ease-out;
}
@keyframes live-flash {
  from { background-color: var(--accent-wash, rgba(120, 160, 255, 0.18)); }
  to   { background-color: transparent; }
}
@media (prefers-reduced-motion: reduce) {
  .live-changed { animation: none; }
}
```

- [ ] **Step 5: Run the server test and the CSS-adjacent suite**

Run: `pytest tests/test_api.py::test_editable_surfaces_are_marked_live_preserve -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bridge/templates/_launch.html src/bridge/static/app.css tests/test_api.py
git commit -m "Protect editable textareas from live morphs and add a changed-value highlight"
```

---

### Task 7: Verify in Arc and run the whole suite

minidom cannot model scroll, focus, or CSS, so the properties that make this "feel alive without clobbering" are only truly verified in a real browser. Use Arc (Mit's browser), driven with the running local panel.

**Files:** none (verification only). Any defect found here is fixed in the owning task's file with a new regression test before this task closes.

- [ ] **Step 1: Run the entire test suite**

Run: `pytest -q`
Expected: all pass (the pre-work baseline was 1238 passing; this adds tests and must not subtract any).

- [ ] **Step 2: Ensure a fresh local panel is serving**

Restart the local Bridge panel on `:8787` if stale (this is pre-authorized — see the "Bridge serve restart authorized" memory). Confirm `/` loads.

- [ ] **Step 3: Arc — project page updates live**

Open a project page in Arc. Trigger an underlying change (e.g. start/stop a session for that project, or make a commit in its repo so the next ~15s reindex bumps `generation`). Within one reindex cycle, confirm the session/agent status, git dirty/ahead, and history reflect the change with NO manual reload and NO full-page flash.

- [ ] **Step 4: Arc — in-flight edits and scroll survive a refresh**

On a project page, scroll down, expand a `<details>`, and start typing a handoff/compose draft (do not save). Trigger an underlying change and let a live refresh land. Confirm: the draft text is intact, the caret/focus is not lost, the `<details>` stays open, and the scroll position holds. This is the core safety property.

- [ ] **Step 5: Arc — schedule, diagnostics, settings**

Visit `/schedule`, `/diagnostics`, `/settings` in turn; trigger a relevant underlying change for each and confirm it reflects live without a reload. Confirm the connection-state strip reads "Live" and moves to "Reconnecting"/"Stale" if the server is stopped.

- [ ] **Step 6: Record a short capture and note results**

Capture a short GIF of the project-page live update with an unsaved draft surviving (proof of the safety property), per the "verify a flash/jank fix at frame rate" and "verify in the user's actual browser" memories. Note any defect, fix it in the owning task with a regression test, and re-run `pytest -q`.

- [ ] **Step 7: Commit any fixes**

```bash
git add -A
git commit -m "Fix live-refresh defects found in Arc verification"
```

(Skip if Steps 3–6 found nothing to fix.)

---

## Self-Review

**Spec coverage:**
- Persistent SSE reused, no second connection → Task 4 fan-out + Task 5 subscription. ✓
- Generic morph-refresh of current fragment → Task 5. ✓
- Hand-rolled minimal keyed morph, no deps → Task 2 (+ Task 1 harness). ✓
- Scroll/focus/`<details>`/input preserved → Task 2 node reuse + Task 5 `ignoreNode` + Task 7 Arc proof. ✓
- Volatile-node protection (handoff/compose, focus) → Task 5 `ignoreNode` + Task 6 markup. ✓
- Route-relevance gating + debounce + safe-defer → Task 5. ✓
- Overview untouched → only additive `live.js` change in Task 4. ✓
- Error handling: keep DOM, no reload → Task 5 `.catch`; morph absent/throws → Global Constraints + Task 5 guard. ✓
- Feel-alive highlight, reduced-motion, connection strip → Task 6 + Task 7 Step 5. ✓
- Testing: morph unit tests, controller tests, fragment routes, Arc → Tasks 2/5/7. ✓
- Out of scope (Overview unification, WebSockets) → honored; not implemented.

**Placeholder scan:** No TBD/TODO. The one deliberately-deferred branch (`workspaceLiveChanged`) ships as an explicit, safe stub with a written condition for completing or leaving it — generation bumps already cover DB-derived content, so the project page is live regardless; the stub only gates the faster per-card live path.

**Type consistency:** `window.bridgeMorph(live, incoming, opts)`, `opts.{key,ignore,onChange}`, `window.bridgeLive.{onFrame,_emit}`, `window.bridgeFragment.parse`, `window.bridgeLiveRefresh.{_onFrame,_refreshNow}`, `El.{getAttributeNames,insertBefore}` — names match across every task that uses them.
