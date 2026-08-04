# Bridge Persistent Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop replacing the document on every navigation — swap only the content region, so the sidebar, scroll position and the SSE connection are never torn down.

**Architecture:** A hand-rolled router intercepts clicks on the five sidebar destinations, fetches a server-rendered fragment, and replaces two DOM nodes (`.shell__body`, `.shell-status`) plus `document.title` and the nav's `aria-current`. Scripts load once and are NEVER re-executed; per-page-view behaviour registers on a `window.bridgePage` enter/leave registry instead. A three-layer test harness lands green before any router code exists.

**Tech Stack:** FastAPI + Jinja2 (server), vanilla ES2020 (client, no build step), pytest, node 20 for JS tests. **No new dependencies are added by this plan.**

Spec: `docs/superpowers/specs/2026-08-04-bridge-persistent-shell-design.md`

## Global Constraints

- **Run tests with `uv run pytest`, NEVER bare `pytest`.** Baseline at plan start: **1025 passing**.
- **NEVER pipe pytest.** This shell is zsh, where `${PIPESTATUS[0]}` is empty (zsh spells it `$pipestatus`, lowercase, 1-indexed). A piped pytest in an `&&` chain has already landed two commits on a red suite in this repo. Redirect to a file and read `$?` directly.
- **Absolute coreutil paths.** `/bin/ls`, `/bin/cat`, `/bin/ps`, `/bin/kill`; `/usr/bin/grep`, `/usr/bin/sed`, `/usr/bin/git`, `/usr/bin/head`, `/usr/bin/tail`, `/usr/sbin/lsof`. **`/usr/bin/ls` DOES NOT EXIST on macOS** — an `||` fallback on it reports a confident false negative.
- **Restart the server after ANY `.py` change** — `bridge serve` reloads templates and `app.css` from disk but keeps Python modules in memory. `/usr/sbin/lsof -t -iTCP:8787 | xargs kill` then `nohup uv run bridge serve &` (no `--port`). **Pre-authorized; never ask.**
- **Every new test is mutation-verified with `tools/falsify.py`.** A survivor means either a vacuous test or an untested invariant. Both must be fixed before the task is done.
- **Node must be found by absolute path.** `tools/falsify.py` runs pytest with `PATH=/usr/bin:/bin`, where Homebrew's node is invisible. Reuse `tests/test_static_js.py:39` `_node()`, which searches `/opt/homebrew/bin/node`, `/usr/local/bin/node`, `/usr/bin/node`. A bare `shutil.which("node")` makes a skipped test look like a passing one and has already produced a false SURVIVED result in this repo.
- **No new npm/node dependency, no `package.json`, no build step.** This is a deliberate property of the repo.
- **Do NOT delete the view-transition CSS** (`app.css`) or its tests in this plan. It goes inert; removing it is separate work after Arc verification.
- **Commit after every task.** No AI attribution, no `Co-Authored-By`, no "Generated with" lines.

---

## File Structure

**Created:**
| File | Responsibility |
|---|---|
| `src/bridge/static/router.js` | Click interception, fetch, swap, history, focus. |
| `src/bridge/templates/_fragment.html` | Fragment layout: emits only the swap payload. |
| `tests/test_shell_contract.py` | Layer 1 — static invariants read off the JS sources. |
| `tests/test_fragment_routes.py` | Layer 2 — fragment contract per route. |
| `tests/test_swap_lifecycle.py` | Layer 3 — multi-document-lifetime tests via node. |
| `tests/js/minidom.js` | The hand-rolled DOM the layer 3 harness runs against. |

**Modified:** `src/bridge/static/shell.js` (hosts the registry), `settings.js`, `schedule.js`, `projects.js`, `launch.js`, `live.js`, `src/bridge/templates/base.html`, the 5 in-scope page templates, `src/bridge/api.py`.

**Untouched:** `_components.html` (shared with the Overview — see the cascade trap), `app.css`, `copy.js`, `project.html`'s route behaviour.

---

## Task 1: The `bridgePage` registry and the mini-DOM harness

Foundation for everything else. Nothing behavioural changes yet.

**Files:**
- Modify: `src/bridge/static/shell.js:13-71`
- Create: `tests/js/minidom.js`
- Create: `tests/test_swap_lifecycle.py`

**Interfaces:**
- Produces: `window.bridgePage.onEnter(fn)`, `window.bridgePage.onLeave(fn)`, `window.bridgePage.enter()`, `window.bridgePage.leave()`. `enter()` runs every registered enter hook in registration order, catching and logging per-hook throws so one bad hook cannot abort the rest. `leave()` does the same for leave hooks.
- Produces: `tests/js/minidom.js` exporting `makeDocument(html)` → a document object supporting `querySelector`, `querySelectorAll`, `getElementById`, `addEventListener`, `dispatchEvent` (with bubbling), and per-element `closest`, `getAttribute`, `setAttribute`, `removeAttribute`, `textContent`, `value`, `dataset`, `hidden`, `options`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_swap_lifecycle.py`. It follows `tests/test_static_js.py`'s precedent exactly — real JS under real node, absolute-path node lookup.

```python
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
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_swap_lifecycle.py -v > /tmp/t.txt 2>&1; echo "EXIT=$?"; /usr/bin/tail -20 /tmp/t.txt
```
Expected: FAIL — `minidom.js` does not exist.

- [ ] **Step 3: Write `tests/js/minidom.js`**

```javascript
// A DOM small enough to read in one sitting, for testing what survives a swap.
//
// SCOPE, stated so nobody mistakes a pass here for a browser pass: this models
// element identity, attribute/class/id/tag selectors (compound and descendant),
// `closest`, and event dispatch that bubbles up the parent chain. That is what
// the swap contract turns on -- whether a listener is still attached, whether a
// captured node went stale, how many times a handler ran.
//
// It does NOT model: layout, CSS, focus, `blur`/`focusout` emitted by node
// removal, or event ordering against the real task queue. Do not add assertions
// that depend on those; add a browser check in Arc instead.
//
// `tests/test_shell_contract.py` asserts that no static file uses a selector
// form this file cannot parse, so the harness can never silently mis-model the
// code under test.

const SELECTOR = /^([a-z][\w-]*)?((?:[#.][\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/i;

function parseSimple(part) {
  const m = SELECTOR.exec(part);
  if (!m) throw new Error(`minidom: unsupported selector part "${part}"`);
  const tests = [];
  if (m[1]) tests.push((el) => el.tag === m[1].toLowerCase());
  const rest = m[2] || "";
  const token = /[#.][\w-]+|\[[\w-]+(?:="[^"]*")?\]/g;
  let t;
  while ((t = token.exec(rest))) {
    const s = t[0];
    if (s[0] === "#") tests.push((el) => el.getAttribute("id") === s.slice(1));
    else if (s[0] === ".") tests.push((el) => el.classList.has(s.slice(1)));
    else {
      const eq = s.indexOf("=");
      if (eq === -1) {
        const name = s.slice(1, -1);
        tests.push((el) => el.getAttribute(name) !== null);
      } else {
        const name = s.slice(1, eq);
        const val = s.slice(eq + 2, -2);
        tests.push((el) => el.getAttribute(name) === val);
      }
    }
  }
  return (el) => tests.every((fn) => fn(el));
}

function compile(selector) {
  // Descendant combinators only -- no `>`, `+`, `~`. Rightmost part matches the
  // candidate; each part to its left must match some ancestor, in order.
  const parts = selector.trim().split(/\s+/).map(parseSimple).reverse();
  return (el) => {
    if (!parts[0](el)) return false;
    let node = el.parent;
    for (let i = 1; i < parts.length; i += 1) {
      while (node && !parts[i](node)) node = node.parent;
      if (!node) return false;
      node = node.parent;
    }
    return true;
  };
}

class El {
  constructor(tag, attrs = {}) {
    this.tag = tag;
    this.attrs = new Map(Object.entries(attrs));
    this.children = [];
    this.parent = null;
    this.listeners = new Map();
    this.classList = new Set((attrs.class || "").split(/\s+/).filter(Boolean));
    this._text = "";
    this.dataset = {};
    this.options = [];
    this.value = attrs.value ?? "";
    this.defaultValue = this.value;
    this.disabled = false;
  }
  get hidden() { return this.getAttribute("hidden") !== null; }
  set hidden(v) { v ? this.setAttribute("hidden", "") : this.removeAttribute("hidden"); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.attrs.delete(name); }
  hasAttribute(name) { return this.attrs.has(name); }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.children = []; }
  append(child) { child.parent = this; this.children.push(child); }
  remove() {
    if (!this.parent) return;
    const i = this.parent.children.indexOf(this);
    if (i >= 0) this.parent.children.splice(i, 1);
    this.parent = null;
  }
  descendants() {
    const out = [];
    for (const c of this.children) { out.push(c, ...c.descendants()); }
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const match = compile(sel);
    return this.descendants().filter(match);
  }
  closest(sel) {
    const match = compile(sel);
    let node = this;
    while (node) { if (node.tag && match(node)) return node; node = node.parent; }
    return null;
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  // Count of handlers for a type, which is how the duplicate-listener tests
  // assert "exactly one" without depending on side effects.
  listenerCount(type) { return (this.listeners.get(type) || []).length; }
  dispatchEvent(event) {
    let node = this;
    event.target = this;
    while (node) {
      for (const fn of node.listeners.get(event.type) || []) fn(event);
      node = node.parent;
    }
  }
}

module.exports = { El, compile, makeDocument, load, report };
```

The remaining three exports:

```javascript
function makeDocument(root) {
  const doc = new El("#document");
  doc.documentElement = new El("html");
  doc.append(doc.documentElement);
  const body = new El("body");
  doc.documentElement.append(body);
  doc.body = body;
  if (root) body.append(root);
  doc.getElementById = (id) => doc.querySelector(`[id="${id}"]`);
  doc.createElement = (tag) => new El(tag);
  globalThis.document = doc;
  globalThis.window = globalThis;
  globalThis.window.matchMedia = () => ({ matches: false, addEventListener() {} });
  globalThis.localStorage = {
    _m: new Map(),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
  };
  // Counting stubs -- the duplicate-hazard tests assert on these.
  globalThis.__calls = { fetch: [], eventSource: [], interval: 0 };
  globalThis.fetch = (url, opts) => {
    globalThis.__calls.fetch.push({ url, opts });
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
  };
  globalThis.EventSource = function (url) {
    globalThis.__calls.eventSource.push(url);
    this.addEventListener = () => {};
    this.close = () => {};
  };
  globalThis.setInterval = () => { globalThis.__calls.interval += 1; return 0; };
  globalThis.setTimeout = () => 0;
  return doc;
}

function load(path) {
  if (!globalThis.document) makeDocument(null);
  // `vm` in the SAME realm on purpose: re-loading a file that declares a
  // top-level `const` must throw here exactly as it would in a browser, because
  // "you cannot re-execute these files" is the constraint the whole design
  // rests on. A fresh realm per file would hide it.
  require("vm").runInThisContext(require("fs").readFileSync(path, "utf8"), { filename: path });
}

function report(obj) { console.log(JSON.stringify(obj)); }
```

- [ ] **Step 4: Add the registry to `shell.js`**

Insert immediately after `if (!document.addEventListener) return;` (currently line 14), before `const root = ...`:

```javascript
  // Bridge swaps only the content region on navigation, so these files are
  // loaded ONCE and never re-executed -- launch.js, live.js and settings.js all
  // declare top-level `const`, and re-evaluating any of them in the same realm
  // throws SyntaxError and aborts the whole file. Anything that must run per
  // page view therefore registers here instead of running at load.
  //
  // Hooks are isolated: one page's broken hook must not stop another page's
  // from running, so each is called in its own try/catch.
  const enterHooks = [];
  const leaveHooks = [];
  function runAll(hooks) {
    for (const fn of hooks) {
      try {
        fn();
      } catch (error) {
        console.error("bridge: page hook failed", error);
      }
    }
  }
  window.bridgePage = {
    onEnter(fn) { enterHooks.push(fn); },
    onLeave(fn) { leaveHooks.push(fn); },
    enter() { runAll(enterHooks); },
    leave() { runAll(leaveHooks); },
  };
```

- [ ] **Step 5: Run to verify it passes**

```bash
uv run pytest tests/test_swap_lifecycle.py -v > /tmp/t.txt 2>&1; echo "EXIT=$?"; /usr/bin/tail -20 /tmp/t.txt
```
Expected: 3 passed.

- [ ] **Step 6: Run the full suite**

```bash
uv run pytest > /tmp/t.txt 2>&1; echo "EXIT=$?"; /usr/bin/tail -3 /tmp/t.txt
```
Expected: `EXIT=0`, 1028 passed.

- [ ] **Step 7: Mutation-verify**

Break each in turn, confirm the named test fails, restore:
1. `enter()` runs hooks in reverse → order test fails.
2. Remove the try/catch → isolation test fails.
3. `leave()` calls `runAll(enterHooks)` → leave test fails.

- [ ] **Step 8: Commit**

```bash
/usr/bin/git add src/bridge/static/shell.js tests/js/minidom.js tests/test_swap_lifecycle.py
/usr/bin/git commit -m "Add the page enter/leave registry and a mini-DOM test harness

Scripts cannot be re-executed after a content swap: launch.js, live.js and
settings.js declare top-level const, so re-evaluating them in the same
realm throws SyntaxError and aborts the file. Per-page-view behaviour will
register on this registry instead of running at load.

The harness models element identity, selectors and bubbling -- what the
swap contract turns on -- and deliberately not layout or focus. It loads
files with runInThisContext rather than a fresh realm per file so that
re-execution throws here exactly as it would in a browser."
```

---

## Task 2: Layer 1 — static invariants

Encodes the §4 rules so the bug class cannot come back. Written FIRST so tasks 3–7 are driven by red tests.

**Files:**
- Create: `tests/test_shell_contract.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed by later tasks; it is a gate.

- [ ] **Step 1: Write the failing tests**

```python
"""The architectural rules a content swap depends on, asserted against the source.

These enforce the RULE rather than simulating its consequences: a file that
captures a DOM node at module scope is broken after the first swap no matter what
any behavioural test happens to exercise. Cheap, fast, and impossible to satisfy
with a vacuous stub.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static"
TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "bridge" / "templates"

PAGE_SCRIPTS = ["shell.js", "copy.js", "launch.js", "schedule.js",
                "live.js", "projects.js", "settings.js", "router.js"]


def source(name: str) -> str:
    """Source with comments stripped.

    Stripping is not cosmetic: these files document themselves using the exact
    identifiers under test, and a previous test in this repo passed because its
    regex matched the PROSE of a comment rather than the code.
    """
    text = (STATIC / name).read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


@pytest.mark.parametrize("name", PAGE_SCRIPTS)
def test_no_domcontentloaded_outside_the_router(name):
    """DOMContentLoaded fires once per DOCUMENT, and there is now one document.

    schedule.js bound its timezone painting to it, so every scheduled time would
    render in raw UTC on any page reached by a swap.
    """
    if name in ("shell.js", "router.js"):
        return
    assert "DOMContentLoaded" not in source(name), (
        f"{name} binds DOMContentLoaded, which fires once per document -- it will "
        f"never fire again after the first swap. Register a bridgePage.onEnter hook."
    )


@pytest.mark.parametrize("name", PAGE_SCRIPTS)
def test_no_module_scope_dom_capture(name):
    """A node captured at load is detached forever after the first swap.

    live.js held `initialStrip` this way: a permanent detached-node leak and a
    trap for any later read of it.
    """
    pattern = re.compile(
        r"^(?:const|let|var)\s+\w+\s*=\s*[^;]*?"
        r"document\.(querySelector|querySelectorAll|getElementById)\b",
        re.M,
    )
    hits = pattern.findall(source(name))
    assert not hits, (
        f"{name} captures a DOM node at module scope ({hits}). That node is "
        f"detached after the first content swap. Look it up inside the handler "
        f"or inside a bridgePage.onEnter hook instead."
    )


@pytest.mark.parametrize("name", ["settings.js", "schedule.js", "projects.js",
                                  "launch.js", "live.js"])
def test_per_page_behaviour_is_registered_on_the_registry(name):
    assert "bridgePage.onEnter" in source(name), (
        f"{name} has per-page-view setup that must re-run after a swap, but "
        f"registers no bridgePage.onEnter hook."
    )


def test_launch_flushes_pending_edits_before_the_swap():
    """Removing a focused node does not fire focusout in ANY browser.

    launch.js saves an edited handoff prompt on focusout. A full document
    navigation fires it; detaching the node does not. Without an onLeave flush
    the user's edit is silently discarded -- and the prompt is the one thing
    Bridge cannot rebuild from transcripts.
    """
    assert "bridgePage.onLeave" in source("launch.js"), (
        "launch.js registers no onLeave hook, so a prompt edited and then "
        "navigated away from is lost with no PATCH and no warning"
    )


def test_no_template_overrides_the_scripts_block():
    """The script-injection path is gone; nothing may reintroduce it.

    A swap never evaluates `{% block scripts %}`, so a page whose JS arrives that
    way is simply inert. settings.js was the only user and now loads from base.
    """
    offenders = [
        p.name for p in TEMPLATES.glob("*.html")
        if p.name != "base.html" and "block scripts" in p.read_text()
    ]
    assert offenders == [], (
        f"{offenders} override the scripts block, which a content swap never "
        f"evaluates -- that page's JS would never run. Load it from base.html."
    )


def test_settings_js_is_loaded_from_base():
    assert "settings.js" in (TEMPLATES / "base.html").read_text()


def test_every_selector_is_one_the_harness_can_parse():
    """Keeps the layer-3 harness honest about the code it claims to model.

    minidom.js supports tag/class/id/attribute parts and descendant combinators.
    If a source file starts using `>`, `+`, `~` or `:pseudo`, the harness would
    silently mis-model it -- so that is a failure here, not a surprise there.
    """
    literal = re.compile(r"""querySelector(?:All)?\(\s*["'`]([^"'`]+)["'`]""")
    unsupported = re.compile(r"[>+~]|::?[a-z-]+\(?")
    bad = []
    for name in PAGE_SCRIPTS:
        if not (STATIC / name).exists():
            continue
        for sel in literal.findall(source(name)):
            if unsupported.search(sel):
                bad.append((name, sel))
    assert bad == [], (
        f"selector forms the mini-DOM cannot parse: {bad}. Either simplify the "
        f"selector or teach tests/js/minidom.js the form -- do not leave the "
        f"harness silently mis-modelling the code under test."
    )
```

- [ ] **Step 2: Run to verify it fails, and record exactly which**

```bash
uv run pytest tests/test_shell_contract.py -v > /tmp/t.txt 2>&1; echo "EXIT=$?"; /usr/bin/grep -E "PASSED|FAILED" /tmp/t.txt
```
Expected failures, which are the work of tasks 3–7:
- `test_no_domcontentloaded_outside_the_router[schedule.js]`
- `test_no_module_scope_dom_capture[live.js]` and `[settings.js]`
- `test_per_page_behaviour_is_registered_on_the_registry` — all five
- `test_launch_flushes_pending_edits_before_the_swap`
- `test_no_template_overrides_the_scripts_block` — `settings.html`
- `test_settings_js_is_loaded_from_base`

`router.js` does not exist yet; the parametrised tests skip it via the `.exists()` guard in the selector test and `PAGE_SCRIPTS` entries are only read through `source()` in tests that guard. **If a test errors on the missing `router.js` rather than failing meaningfully, add the same `.exists()` guard to it.**

- [ ] **Step 3: Mark the known-red tests**

Do NOT leave the suite red across tasks. Add at the top of the module:

```python
# Tasks 3-7 of docs/superpowers/plans/2026-08-04-bridge-persistent-shell.md turn
# each of these green, one file per task. Each xfail is removed by the task that
# fixes its file -- `strict=True` means a premature fix fails loudly rather than
# passing silently, so none of these can be quietly forgotten.
pytestmark_known_red = pytest.mark.xfail(strict=True, reason="fixed in tasks 3-7")
```

Apply `@pytestmark_known_red` to each currently-failing test listed above.

- [ ] **Step 4: Verify the suite is green**

```bash
uv run pytest > /tmp/t.txt 2>&1; echo "EXIT=$?"; /usr/bin/tail -3 /tmp/t.txt
```
Expected: `EXIT=0`, with the xfails reported as xfailed, not failed.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add tests/test_shell_contract.py
/usr/bin/git commit -m "Assert the swap contract against the JS sources

Enforces the rule rather than simulating its consequences: a file that
captures a DOM node at module scope is broken after the first swap
regardless of what any behavioural test happens to exercise.

The invariants each file currently violates are marked xfail(strict) and
are turned green one file per commit, so a premature fix fails loudly
instead of passing silently."
```

---

## Task 3: `settings.js` — load from base, delegate the change handler

Fixes the sharpest breakage: `/settings` is completely inert when reached by a swap.

**Files:**
- Modify: `src/bridge/static/settings.js:63-78`
- Modify: `src/bridge/templates/base.html:165`
- Modify: `src/bridge/templates/settings.html` (remove the `scripts` block)
- Modify: `tests/test_shell_contract.py` (drop 3 xfails)

**Interfaces:**
- Consumes: `window.bridgePage.onEnter` from Task 1.
- Produces: nothing.

- [ ] **Step 1: Remove the three xfail markers**

Drop `@pytestmark_known_red` from `test_no_module_scope_dom_capture[settings.js]` (i.e. remove `settings.js` from any xfail parametrisation), `test_no_template_overrides_the_scripts_block`, and `test_settings_js_is_loaded_from_base`. Also drop the `settings.js` entry from the registry test's xfail.

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_shell_contract.py -v > /tmp/t.txt 2>&1; echo "EXIT=$?"; /usr/bin/grep FAILED /tmp/t.txt
```
Expected: those four FAIL.

- [ ] **Step 3: Rewrite `bindSelect` as delegation**

Replace `settings.js:61-78` entirely:

```javascript
// One delegated `change` listener for all five selects, registered once. A
// direct `el.addEventListener` would die with the node the moment the content
// region is swapped, leaving the controls looking fine and silently saving
// nothing.
const SELECT_KEYS = [
  ["[data-settings-theme]", APPEARANCE_KEY, applyTheme],
  ["[data-settings-density]", DENSITY_KEY, applyDensity],
  ["[data-settings-launch-model]", LAUNCH_MODEL_KEY, null],
  ["[data-settings-launch-effort]", LAUNCH_EFFORT_KEY, null],
  ["[data-settings-launch-mode]", LAUNCH_MODE_KEY, null],
];

document.addEventListener("change", (event) => {
  if (!event.target || !event.target.closest) return;
  for (const [selector, key, onChange] of SELECT_KEYS) {
    const el = event.target.closest(selector);
    if (!el) continue;
    localStorage.setItem(key, el.value);
    if (onChange) onChange();
    return;
  }
});

// Restore stored values into the selects on every page view, leaving the
// server-rendered default alone where nothing is stored yet. This has to re-run
// after a swap: the selects are fresh nodes carrying the server's defaults, so
// without it Settings shows values the user did not choose.
function restoreSettingsSelects() {
  for (const [selector, key] of SELECT_KEYS) {
    const el = document.querySelector(selector);
    if (!el) continue;
    const stored = localStorage.getItem(key);
    if (stored !== null) el.value = stored;
  }
}

if (window.bridgePage) window.bridgePage.onEnter(restoreSettingsSelects);
else restoreSettingsSelects();
```

Leave lines 1–59 (`applyTheme`, `applyDensity`, the `darkMedia` listener) untouched — they act on `<html>`, which never swaps, and the media listener must stay registered exactly once.

- [ ] **Step 4: Move the script tag**

In `src/bridge/templates/settings.html`, delete the whole `{% block scripts %}...{% endblock %}` (around line 142).

In `src/bridge/templates/base.html`, after the `projects.js` tag at line 164, add:

```html
  {# Loaded for every page, not just /settings. Bridge swaps only the content
     region, so `{% block scripts %}` is never evaluated on a swapped
     navigation -- a page whose JS arrived that way would simply be inert.
     Scoped to `data-settings-*` elements, so it is a no-op everywhere else. #}
  <script src="/static/settings.js" defer></script>
```

Then delete the now-unused `{% block scripts %}{% endblock %}` at line 165.

- [ ] **Step 5: Run to verify they pass**

```bash
uv run pytest tests/test_shell_contract.py tests/test_settings.py -v > /tmp/t.txt 2>&1; echo "EXIT=$?"; /usr/bin/tail -5 /tmp/t.txt
```
Expected: all pass.

- [ ] **Step 6: Full suite + server restart**

```bash
/usr/sbin/lsof -t -iTCP:8787 | xargs kill 2>/dev/null; nohup uv run bridge serve > /tmp/bridge.log 2>&1 &
uv run pytest > /tmp/t.txt 2>&1; echo "EXIT=$?"; /usr/bin/tail -3 /tmp/t.txt
```
Expected: `EXIT=0`.

- [ ] **Step 7: Mutation-verify** — revert the delegated listener to `el.addEventListener` and confirm the module-scope-capture test fails; re-add the `scripts` block to `settings.html` and confirm that test fails.

- [ ] **Step 8: Commit**

```bash
/usr/bin/git add src/bridge/static/settings.js src/bridge/templates/base.html src/bridge/templates/settings.html tests/test_shell_contract.py
/usr/bin/git commit -m "Load settings.js for every page and delegate its change handler

settings.js loaded only via {% block scripts %}, which a content swap
never evaluates, so /settings reached by a swap would be entirely inert --
no stored theme, density or launch defaults restored, no change persisted.

bindSelect also captured each select in a closure at load. Those nodes are
detached after the first swap, so the controls would look right and
silently save nothing. One delegated change listener replaces all five."
```

---

## Task 4: `schedule.js` — drop `DOMContentLoaded`

Without this, every scheduled time renders in raw UTC on any page reached by a swap.

**Files:**
- Modify: `src/bridge/static/schedule.js:188`
- Modify: `tests/test_shell_contract.py` (drop 2 xfails)
- Modify: `tests/test_swap_lifecycle.py` (add 1 test)

- [ ] **Step 1: Remove the xfails** for `test_no_domcontentloaded_outside_the_router[schedule.js]` and the `schedule.js` entry of the registry test.

- [ ] **Step 2: Add a lifecycle test** to `tests/test_swap_lifecycle.py`:

```python
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
```

- [ ] **Step 3: Run to verify it fails**

```bash
uv run pytest tests/test_swap_lifecycle.py tests/test_shell_contract.py -v > /tmp/t.txt 2>&1; echo "EXIT=$?"; /usr/bin/grep FAILED /tmp/t.txt
```

- [ ] **Step 4: Replace line 188**

`paintScheduledTimes` already takes an optional `root` (line 67), so no signature change is needed. Replace:

```javascript
document.addEventListener("DOMContentLoaded", () => paintScheduledTimes());
```

with:

```javascript
// Repaint on every page view, not once per document. Bridge swaps only the
// content region, so DOMContentLoaded fires exactly once for the whole session
// -- every scheduled cell rendered by a later swap would keep the server's raw
// UTC string. Registered defensively so this file still works if loaded alone.
if (window.bridgePage) window.bridgePage.onEnter(() => paintScheduledTimes());
else document.addEventListener("DOMContentLoaded", () => paintScheduledTimes());
```

- [ ] **Step 5: Run to verify it passes.** Expected: all pass.

- [ ] **Step 6: Full suite.** Expected `EXIT=0`.

- [ ] **Step 7: Mutation-verify** — restore the bare `DOMContentLoaded` binding and confirm both the static test and the lifecycle test fail.

- [ ] **Step 8: Commit**

```bash
/usr/bin/git add src/bridge/static/schedule.js tests/test_shell_contract.py tests/test_swap_lifecycle.py
/usr/bin/git commit -m "Repaint scheduled times on every page view

DOMContentLoaded fires once per document, and a persistent shell has one.
paintScheduledTimes is the only thing that turns the server's stored epoch
seconds into the viewer's own clock, so every scheduled cell rendered by a
swap would show a raw UTC string."
```

---

## Task 5: `projects.js` — re-derive the filter and the view toggle

**Files:**
- Modify: `src/bridge/static/projects.js:111`, `:303-312`
- Modify: `tests/test_shell_contract.py`, `tests/test_swap_lifecycle.py`

- [ ] **Step 1: Remove the `projects.js` xfail** from the registry test.

- [ ] **Step 2: Add a lifecycle test**

```python
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
```

- [ ] **Step 3: Run to verify it fails.**

- [ ] **Step 4: Replace lines 303-312**

```javascript
// Both run on every page view, not once at load. The server renders the "all"
// filter with no query, and projects.html always renders List as the pressed
// button -- these two calls are what reconcile that with the stored preference
// and the live search box. Run once, a swapped navigation keeps the server's
// text and announces the wrong button.
if (window.bridgePage) {
  window.bridgePage.onEnter(() => {
    applyProjectsFilter();
    applyProjectsView(currentProjectsView());
  });
} else {
  applyProjectsFilter();
  applyProjectsView(currentProjectsView());
}
```

- [ ] **Step 5: Route the hard navigation at line 111 through the router**

`projects.js:111` currently calls `window.location.assign("/projects")`, which force-reloads every script and defeats the shell. Replace it with:

```javascript
    // Go through the router when it is present so the shell survives; a hard
    // assign would tear down the SSE connection and reload every script.
    if (window.bridgeNavigate) window.bridgeNavigate("/projects");
    else window.location.assign("/projects");
```

`window.bridgeNavigate` is defined in Task 9. The `else` branch keeps this correct until then and forever after if JS for the router fails to load.

- [ ] **Step 6: Run to verify it passes. Step 7: Full suite.**

- [ ] **Step 8: Mutation-verify** — move the two calls back to top-level and confirm the lifecycle test fails.

- [ ] **Step 9: Commit**

```bash
/usr/bin/git add src/bridge/static/projects.js tests/test_shell_contract.py tests/test_swap_lifecycle.py
/usr/bin/git commit -m "Re-derive the projects filter and view toggle on every page view

The template always renders List as pressed and relies on this correction
from the stored data-projects-view attribute; run once at load, a swapped
navigation shows a grid while both buttons announce List, pressed.

The restore path's window.location.assign now prefers the router when it
is present, so hiding a project no longer force-reloads the whole shell."
```

---

## Task 6: `launch.js` — prefill per view, and flush edits before the swap

Contains the data-loss fix. **The most important task in the plan.**

**Files:**
- Modify: `src/bridge/static/launch.js:33-60`, `:226-256`
- Modify: `tests/test_shell_contract.py`, `tests/test_swap_lifecycle.py`

- [ ] **Step 1: Remove the `launch.js` xfails** from the registry test and from `test_launch_flushes_pending_edits_before_the_swap`.

- [ ] **Step 2: Add the data-loss lifecycle test**

```python
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
```

- [ ] **Step 3: Run to verify both fail.**

- [ ] **Step 4: Extract the save, and register both hooks**

Refactor `launch.js:36-60`. Pull the body of the `focusout` handler into a named function so the leave hook can reuse it — the two paths must not drift:

```javascript
// Save an edited prompt when focus leaves the field, and only when the text
// actually changed — `focusout` (which bubbles, unlike `blur`) fires on every
// tab-through, and a PATCH per tab-through would re-journal an unchanged prompt.
async function savePrompt(field) {
  const handoffId = field.getAttribute("data-prompt-handoff");
  const saved = field.dataset.savedPrompt ?? field.defaultValue;
  if (field.value === saved) return;

  const key = `[data-prompt-status="${field.id}"]`;
  try {
    const response = await fetch(`/api/handoff/${encodeURIComponent(handoffId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ next_prompt: field.value }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    field.dataset.savedPrompt = field.value;
    announce(key, "✓ Prompt saved");
  } catch (error) {
    // The prompt is the one thing Bridge cannot rebuild from transcripts, so a
    // failed save says so in words and points at the way out.
    console.error("bridge: saving the prompt failed", error);
    announce(key, "⚠ Not saved — use Copy prompt so the text is not lost");
  }
}

document.addEventListener("focusout", (event) => {
  const field = event.target.closest("[data-prompt-handoff]");
  if (field) savePrompt(field);
});
```

Then at the end of the file:

```javascript
// Detaching a focused node does NOT fire focusout in any browser -- a full
// document navigation did, which is why this was safe before the shell
// persisted. Without this flush an edit made and then navigated away from is
// discarded silently, and the prompt cannot be rebuilt from transcripts.
//
// Deliberately not awaited: the swap must not be held up by the network, and
// `savePrompt` already reports its own failure into the status line.
if (window.bridgePage) {
  window.bridgePage.onLeave(() => {
    document.querySelectorAll("[data-prompt-handoff]").forEach(savePrompt);
  });
  window.bridgePage.onEnter(prefillLaunchDefaults);
}
```

- [ ] **Step 5: Make `prefillLaunchDefaults` callable**

Change line 226 from an IIFE to a named declaration. Replace `(function prefillLaunchDefaults() {` with `function prefillLaunchDefaults() {`, and the closing `})();` at line 256 with `}`. Add a call for the no-registry case:

```javascript
if (!window.bridgePage) prefillLaunchDefaults();
```

- [ ] **Step 6: Run to verify both pass. Step 7: Full suite.**

- [ ] **Step 8: Mutation-verify** — remove the `onLeave` registration (data-loss test fails); drop the `if (field.value === saved) return;` guard (unchanged-prompt test fails); leave `prefillLaunchDefaults` an IIFE (registry test fails).

- [ ] **Step 9: Commit**

```bash
/usr/bin/git add src/bridge/static/launch.js tests/test_shell_contract.py tests/test_swap_lifecycle.py
/usr/bin/git commit -m "Flush an edited prompt before the content is swapped away

Removing a focused node does not fire focusout in any browser. A full
document navigation did, which is what made the focusout save sufficient
until now; detaching the node does not, so an edit made and then navigated
away from would be discarded with no PATCH and no warning. The prompt is
the one thing Bridge cannot rebuild from transcripts.

The focusout handler and the leave flush now share one savePrompt, so the
change guard cannot drift between them, and the launch defaults prefill
re-runs per page view instead of once per document."
```

---

## Task 7: `live.js` — re-seed the freshness state per page view

**Files:**
- Modify: `src/bridge/static/live.js:183-193`, `:281-292`
- Modify: `tests/test_shell_contract.py`, `tests/test_swap_lifecycle.py`

- [ ] **Step 1: Remove the `live.js` xfails** from `test_no_module_scope_dom_capture` and the registry test.

- [ ] **Step 2: Add two lifecycle tests**

```python
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
```

- [ ] **Step 3: Run to verify they fail.**

- [ ] **Step 4: Extract the boot block into a re-runnable hook**

Replace `live.js:281-292` with:

```javascript
// Re-seeded on every page view. The freshness strip only exists on the Overview
// and each swap inserts a brand-new, server-rendered one, so the baselines below
// have to be read again from the node that is actually on screen.
function bootFreshness() {
  lastIndexAt = initialIndexAt();
  const strip = query("[data-freshness-strip]");
  if (!strip) return;
  // `getAttribute` returns `null` for a missing attribute (Overview's strip
  // never renders `data-generation`), and `Number(null)` is 0 -- a real,
  // finite generation, not the "unknown" `patchFreshness` needs it to mean.
  // Reading the raw value first keeps "absent" and "present as 0" distinct.
  const rawGeneration = strip.getAttribute("data-generation");
  const initialGeneration = rawGeneration == null ? NaN : Number(rawGeneration);
  lastGeneration = Number.isFinite(initialGeneration) ? initialGeneration : null;
  // Clear the cached state FIRST. `announceConnectionState` returns early when
  // the state it is handed matches the cache, and that cache drifts on while the
  // user is on a page that has no strip at all -- so without this reset the
  // freshly-swapped strip is never written to and freezes at whatever the server
  // rendered.
  lastConnectionState = null;
  announceConnectionState(
    connectionState(strip.getAttribute("data-server"), Math.floor(Date.now() / 1000)),
  );
}

if (window.bridgePage) window.bridgePage.onEnter(bootFreshness);
else bootFreshness();
```

Note `initialStrip` is gone entirely — it was a module-scope capture holding a detached node forever.

- [ ] **Step 5: Run to verify they pass. Step 6: Full suite.**

- [ ] **Step 7: Mutation-verify** — drop the `lastConnectionState = null` line (re-seed test fails); move `connect()` inside the enter hook (EventSource test fails); restore `const initialStrip` (static capture test fails).

- [ ] **Step 8: Commit**

```bash
/usr/bin/git add src/bridge/static/live.js tests/test_shell_contract.py tests/test_swap_lifecycle.py
/usr/bin/git commit -m "Re-seed the freshness baselines on every page view

The boot block read the strip's index and generation once per document and
was not callable. Each swap inserts a new server-rendered strip, so those
baselines have to be re-read from the node actually on screen.

announceConnectionState returns early when the state matches its cache, and
that cache keeps drifting while the user is on a page with no strip at all
-- so the reset is what lets the swapped-in strip be corrected instead of
freezing at whatever the server rendered. The EventSource and the age
ticker stay outside the hook: surviving navigation is the point."
```

---

## Task 8: The fragment route

Server half. Still no router — the fragment is proven by tests before anything fetches it.

**Files:**
- Create: `src/bridge/templates/_fragment.html`
- Modify: `src/bridge/templates/overview.html`, `projects.html`, `schedule.html`, `settings.html`, `diagnostics.html` (one line each)
- Modify: `src/bridge/api.py` (the five in-scope routes)
- Create: `tests/test_fragment_routes.py`

**Interfaces:**
- Produces: `GET <route>` with header `X-Bridge-Fragment: 1` → HTML containing `<title>`, `div.shell__body`, `div.shell-status`, and `<meta name="bridge-active" content="<key>">`.
- Produces: `_layout_for(request) -> str` in `api.py`, returning `"_fragment.html"` or `"base.html"`.

- [ ] **Step 1: Write the failing tests**

```python
"""The fragment contract: what the router is allowed to assume it will receive.

The full-document assertion matters as much as the fragment one. The 156 existing
route tests all render full documents, and they stay valid only while a request
WITHOUT the fragment header is byte-for-byte what it always was.
"""

import pytest
from fastapi.testclient import TestClient

from bridge.api import create_app
from bridge.config import load
from bridge.models import SessionRecord
from bridge.store import Store

ROUTES = [("/", "overview"), ("/projects", "projects"), ("/schedule", "schedule"),
          ("/diagnostics", "diagnostics"), ("/settings", "settings")]
FRAGMENT = {"X-Bridge-Fragment": "1"}


# There is NO global `client` fixture in conftest.py -- `tests/test_api.py:19`
# defines a local one that yields a 3-tuple `(TestClient, store, pid)`. This
# module needs only the client, so it builds its own rather than importing a
# fixture whose shape it does not use. The autouse guards in conftest.py that
# keep tests off the real Bridge directory apply here regardless.
@pytest.fixture
def c(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo",
                      title="Did the work", ended_at="2026-07-30T10:00:00.000Z",
                      model="claude-opus-5", effort="high", tokens_in=5,
                      tokens_out=5),
        pid,
    )
    yield TestClient(create_app(store, cfg)), pid
    store.close()


@pytest.mark.parametrize("path,active", ROUTES)
def test_fragment_carries_every_swap_target(c, path, active):
    body = c[0].get(path, headers=FRAGMENT).text
    assert "<title>" in body
    assert 'class="shell__body"' in body
    assert 'class="shell-status"' in body
    assert f'content="{active}"' in body


@pytest.mark.parametrize("path,active", ROUTES)
def test_fragment_is_not_a_whole_document(c, path, active):
    body = c[0].get(path, headers=FRAGMENT).text
    assert "<!doctype html>" not in body.lower()
    assert "<aside" not in body, (
        "the fragment carries the sidebar, so a swap would replace the very "
        "chrome this design exists to keep"
    )
    assert "app.css" not in body


@pytest.mark.parametrize("path,active", ROUTES)
def test_a_request_without_the_header_is_unchanged(c, path, active):
    body = c[0].get(path).text
    assert "<!doctype html>" in body.lower()
    assert "<aside" in body
    assert "app.css" in body


@pytest.mark.parametrize("path,active", ROUTES)
def test_the_fragment_body_matches_the_full_documents_body(c, path, active):
    """base.html and _fragment.html each spell out the .shell__body markup.

    Jinja blocks do not survive an `{% include %}`, so the two layouts cannot
    share that markup by factoring. This asserts they never drift instead --
    which is the real risk, because drift would be invisible until a user hit
    the one page whose header the fragment forgot.
    """
    full = c[0].get(path).text
    frag = c[0].get(path, headers=FRAGMENT).text
    marker = '<div class="shell__body">'
    assert full[full.index(marker):].split("</div>")[0][:400] \
        == frag[frag.index(marker):].split("</div>")[0][:400]


def test_the_project_detail_route_has_no_fragment_mode(c):
    """Out of scope by design: the router never intercepts a link to it."""
    client, pid = c
    body = client.get(f"/project/{pid}", headers=FRAGMENT).text
    assert "<!doctype html>" in body.lower()
```

The `/project/{id}` test uses the fixture's real `pid` rather than a hardcoded `1`, so it exercises a 200 rather than passing vacuously on a 404 page that also has a doctype.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Create `_fragment.html`**

```jinja
{# The swap payload, and nothing else: no doctype, no <head>, no sidebar.
   Bridge's router replaces exactly these two nodes and reads the title and the
   active nav key off this response.

   The `.shell__body` markup below is spelled out a second time -- base.html has
   the same block structure. Jinja blocks do not survive an `{% include %}`, so
   the two layouts cannot share it by factoring. `test_fragment_routes.py`
   asserts the two renders stay identical, which is what makes the duplication
   safe rather than a drift waiting to happen. #}
<title>{% block title %}Bridge{% endblock %}</title>
<meta name="bridge-active" content="{{ active }}">
<div class="shell__body">
  <header class="page-head">
    <div class="page-head__text">
      {% block page_eyebrow %}{% endblock %}
      <h1 class="page-title">{% block page_title %}Bridge{% endblock %}</h1>
      {% block page_summary %}{% endblock %}
    </div>
    <div class="page-actions">
      <a class="risk" href="/diagnostics" data-diagnostics-alert
         {% if not diag_alert %}hidden{% endif %}>{% if diag_alert %}⚠ {% endif %}Diagnostics</a>
      {% block page_actions %}{% endblock %}
    </div>
  </header>
  <main id="main" tabindex="-1">{% block content %}{% endblock %}</main>
</div>
<div class="shell-status">
  {% block shell_status %}
    <span class="shell-status__label">Local control plane</span>
    <span class="shell-status__detail">Repository access · read only</span>
  {% endblock %}
</div>
```

- [ ] **Step 4: Make the five page templates layout-agnostic**

In each of `overview.html`, `projects.html`, `schedule.html`, `settings.html`, `diagnostics.html`, change the first line from `{% extends "base.html" %}` to:

```jinja
{% extends layout|default("base.html") %}
```

Leave `project.html` on `{% extends "base.html" %}` — out of scope.

- [ ] **Step 5: Add layout selection in `api.py`**

Above `create_app`:

```python
FRAGMENT_HEADER = "x-bridge-fragment"


def _layout_for(request: Request) -> str:
    """Which layout a page template extends.

    A request without the header renders exactly what it always did, which is
    what keeps the existing route tests a true statement about the app.
    """
    return "_fragment.html" if request.headers.get(FRAGMENT_HEADER) else "base.html"
```

Then add `"layout": _layout_for(request),` to the context dict of each of the five in-scope routes: `diagnostics_view` (~line 758), `dashboard` (~783), `projects_view` (~801), `schedule_view_route` (~852), `settings_route` (~864). **Do NOT add it to `detail` (~823).**

Note `schedule_view_route` passes its context inline on one line — expand it to a multi-line dict rather than appending awkwardly.

- [ ] **Step 6: Restart the server, run to verify it passes.**

```bash
/usr/sbin/lsof -t -iTCP:8787 | xargs kill 2>/dev/null; nohup uv run bridge serve > /tmp/bridge.log 2>&1 &
uv run pytest tests/test_fragment_routes.py -v > /tmp/t.txt 2>&1; echo "EXIT=$?"; /usr/bin/tail -5 /tmp/t.txt
```

- [ ] **Step 7: Full suite.** All 156 existing route tests must still pass untouched — that is the point of `_layout_for`'s default.

- [ ] **Step 8: Mutation-verify** — make `_layout_for` always return `_fragment.html` (the unchanged-document tests fail); drop `shell_status` from `_fragment.html` (the swap-target test fails); change a word in `_fragment.html`'s `page-head` (the anti-drift test fails).

- [ ] **Step 9: Commit**

```bash
/usr/bin/git add src/bridge/templates/_fragment.html src/bridge/templates/*.html src/bridge/api.py tests/test_fragment_routes.py
/usr/bin/git commit -m "Serve a content fragment when the request asks for one

The five sidebar destinations render through an alternate layout carrying
only the swap payload: title, active nav key, .shell__body and
.shell-status. A request without the header renders exactly what it always
did, which is what keeps the 156 existing route tests a true statement.

base.html and _fragment.html each spell out the .shell__body markup
because Jinja blocks do not survive an include. A test asserts the two
renders stay identical rather than trusting them to."
```

---

## Task 9: The router

**Files:**
- Create: `src/bridge/static/router.js`
- Modify: `src/bridge/templates/base.html` (script tag)
- Modify: `tests/test_shell_contract.py`, `tests/test_swap_lifecycle.py`

**Interfaces:**
- Consumes: `window.bridgePage.enter/leave`, the Task 8 fragment.
- Produces: `window.bridgeNavigate(href)` — used by `projects.js:111` from Task 5.

- [ ] **Step 1: Write the failing tests**

```python
def test_router_exposes_navigate_for_in_app_redirects(tmp_path):
    got = run_js(
        'report({ has: typeof window.bridgeNavigate });',
        ["shell.js", "router.js"],
        tmp_path,
    )
    assert got["has"] == "function"
```

And a static one in `tests/test_shell_contract.py`:

```python
def test_router_only_intercepts_the_sidebar_destinations():
    """Widening this silently is how /project/{id} would start swapping.

    That route has no fragment mode (Task 8), so intercepting a link to it would
    swap in a whole document -- sidebar and all -- into the content region.
    """
    text = source("router.js")
    assert "SWAPPABLE" in text
    for path in ('"/"', '"/projects"', '"/schedule"', '"/diagnostics"', '"/settings"'):
        assert path in text
    assert "/project/" not in text


def test_router_falls_back_to_a_normal_navigation():
    text = source("router.js")
    assert "location.assign" in text, (
        "the router must fall back to a real navigation on any failure, or a "
        "server error leaves the user on a page whose link did nothing"
    )
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Write `src/bridge/static/router.js`**

```javascript
// Swap the content region instead of replacing the document.
//
// Bridge is server-rendered, so every nav click used to tear down and rebuild
// the entire page -- including the SSE connection, the scroll position, and all
// module state. Replacing one dense page with another in a single frame is the
// abrupt whole-screen change this exists to remove; no amount of CSS could fix
// it, because the teardown IS the effect.
//
// Progressive enhancement is not decoration here. Every link stays an ordinary
// link: this file only ever calls preventDefault on a navigation it is certain
// it can complete, and ANY failure falls back to a real one.

const SWAPPABLE = new Set(["/", "/projects", "/schedule", "/diagnostics", "/settings"]);

function swappable(url) {
  return url.origin === window.location.origin && SWAPPABLE.has(url.pathname);
}

function parseFragment(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const body = doc.querySelector(".shell__body");
  const status = doc.querySelector(".shell-status");
  const active = doc.querySelector('meta[name="bridge-active"]');
  if (!body || !status) return null;
  return {
    body,
    status,
    title: doc.querySelector("title") ? doc.querySelector("title").textContent : null,
    active: active ? active.getAttribute("content") : null,
  };
}

function setActiveNav(active) {
  // The nav lives in the persistent sidebar, so nothing else would ever correct
  // it -- aria-current would stay on whichever page the session started with.
  document.querySelectorAll(".sidebar__link").forEach((link) => {
    link.removeAttribute("aria-current");
  });
  if (!active) return;
  const current = document.querySelector(`.sidebar__link[href="${activeHref(active)}"]`);
  if (current) current.setAttribute("aria-current", "page");
}

function activeHref(active) {
  return active === "overview" ? "/" : `/${active}`;
}

function applyFragment(parsed) {
  const body = document.querySelector(".shell__body");
  const status = document.querySelector(".shell-status");
  if (!body || !status) return false;
  body.replaceWith(parsed.body);
  status.replaceWith(parsed.status);
  if (parsed.title) document.title = parsed.title;
  setActiveNav(parsed.active);
  return true;
}

// A swap moves neither focus nor the screen reader's attention -- the browser
// does that for a real navigation and does nothing for a DOM replacement. Both
// are required, not polish.
function announceArrival() {
  const main = document.getElementById("main");
  if (main && main.focus) main.focus();
  window.scrollTo(0, 0);
}

async function navigate(href, { push = true } = {}) {
  const url = new URL(href, window.location.href);
  if (!swappable(url)) { window.location.assign(href); return; }
  try {
    window.bridgePage.leave();
    const response = await fetch(url.href, {
      headers: { "X-Bridge-Fragment": "1" },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const parsed = parseFragment(await response.text());
    if (!parsed || !applyFragment(parsed)) throw new Error("unusable fragment");
    if (push) window.history.pushState({ bridge: true }, "", url.href);
    window.bridgePage.enter();
    announceArrival();
  } catch (error) {
    // Never strand the user on a link that did nothing. A real navigation is
    // always correct -- it is only ever slower.
    console.error("bridge: swap failed, falling back to a full load", error);
    window.location.assign(href);
  }
}

window.bridgeNavigate = navigate;

if (document.addEventListener) {
  document.addEventListener("click", (event) => {
    // The standard opt-outs: a modified or non-primary click must keep its
    // browser meaning, or the router breaks cmd-click-to-new-tab.
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest && event.target.closest("a[href]");
    if (!link) return;
    if (link.hasAttribute("download") || link.hasAttribute("target")) return;
    const url = new URL(link.getAttribute("href"), window.location.href);
    if (!swappable(url)) return;
    event.preventDefault();
    navigate(url.href);
  });

  window.addEventListener("popstate", () => {
    navigate(window.location.href, { push: false });
  });
}
```

- [ ] **Step 4: Load it from `base.html`**

Immediately after the `shell.js` tag (line 156), so the registry exists first:

```html
  <script src="/static/router.js" defer></script>
```

- [ ] **Step 5: Run to verify it passes. Step 6: Full suite.**

- [ ] **Step 7: Mutation-verify** — remove the modifier-key guard and assert a cmd-click test fails (add one if absent); remove the `catch` fallback and confirm the static fallback test fails; add `/project/` to `SWAPPABLE` and confirm the scope test fails.

- [ ] **Step 8: Commit**

```bash
/usr/bin/git add src/bridge/static/router.js src/bridge/templates/base.html tests/test_router.py tests/test_shell_contract.py tests/test_swap_lifecycle.py
/usr/bin/git commit -m "Swap the content region instead of replacing the document

Clicks on the five sidebar destinations now fetch a fragment and replace
.shell__body and .shell-status, so the sidebar, the scroll position and
the SSE connection survive the navigation.

Every link stays an ordinary link: preventDefault is called only for a
navigation the router is certain it can complete, modified and
non-primary clicks keep their browser meaning, and any failure falls back
to a real navigation. A swap moves neither focus nor the screen reader's
attention on its own, so it moves focus to <main> explicitly."
```

---

## Task 10: Remove the last xfails and prove the whole lifecycle

**Files:** `tests/test_shell_contract.py`, `tests/test_swap_lifecycle.py`

- [ ] **Step 1: Delete `pytestmark_known_red` and every remaining reference.** If any test still fails without its marker, that file's task was not actually finished — go back and finish it rather than restoring the marker.

- [ ] **Step 2: Add the end-to-end lifecycle test**

```python
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
```

- [ ] **Step 3: Run the full suite. Step 4: Run the whole `tools/falsify.py` sweep** across all new tests from tasks 1–9 and fix any survivor.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add tests/
/usr/bin/git commit -m "Prove five page views leave one connection and one of each listener

Every duplicate hazard in this app is a doubled delegated listener: two
POST /api/launch is two spawned terminal sessions, two POST /api/schedule
is two scheduled rows. Counting listeners catches all of them at once."
```

---

## Task 11: Verify in Arc, and re-run the Overview cascade gate

**No code.** This is the acceptance gate. **Do not skip it and do not substitute the devtools Chrome** — measuring in Chrome while Mit was in Arc is the single omission that cost two prior sessions.

- [ ] **Step 1: Restart the server, confirm it is serving the new files**

```bash
/usr/sbin/lsof -t -iTCP:8787 | xargs kill 2>/dev/null; nohup uv run bridge serve > /tmp/bridge.log 2>&1 &
/usr/bin/curl -s http://127.0.0.1:8787/static/router.js | /usr/bin/head -3
/usr/bin/curl -s -H "X-Bridge-Fragment: 1" http://127.0.0.1:8787/projects | /usr/bin/head -3
```
Expected: the router source, and a fragment with no doctype.

- [ ] **Step 2: Drive Arc via osascript**

Arc is fully scriptable and can run JS. Use `String.raw` inside the JS for selectors containing quotes. **Arc AppleEvents time out intermittently — retry rather than concluding failure.**

```bash
osascript -e 'tell application "Arc" to tell front window to tell active tab to execute javascript "JSON.stringify({href: location.href, sse: !!window.bridgeLiveSource, nav: !!window.bridgeNavigate})"'
```

- [ ] **Step 3: Assert each of these in Arc, hard reloading first**

Anything spanning a navigation needs a listener armed BEFORE it that writes to `sessionStorage`; an injected listener dies with the document it was injected into.

- [ ] Clicking all five destinations never repaints the sidebar (arm a `MutationObserver` on `aside.sidebar` writing to `sessionStorage`; expect zero childList mutations).
- [ ] `performance.getEntriesByType("navigation").length` stays **1** across five clicks — proof no document was replaced.
- [ ] One `EventSource` for the whole session.
- [ ] `/settings` reached BY A SWAP: change the theme select, confirm it persists and re-applies.
- [ ] `/schedule` reached BY A SWAP: scheduled times show local time, not UTC.
- [ ] `/projects` reached BY A SWAP with `localStorage["bridge.projectsView"]="grid"`: grid renders AND the grid button reports `aria-pressed="true"`. **Restore the value to `list` afterwards.**
- [ ] Back/forward buttons work and land on the right page with the right `aria-current`.
- [ ] Cmd-click a nav link still opens a new tab.
- [ ] Edit a handoff prompt, navigate away without blurring, return — the edit survived (this is the data-loss fix).
- [ ] With JS disabled, all five links still navigate normally.

- [ ] **Step 4: The Overview cascade gate — the standing regression gate**

`overview.html:189` renders `<ul class="projects-list">` with `<li class="projects-list__item">`, so the Overview shares CSS class names with `/projects`. Re-measure the Overview's recent rows in **dark** at **1440** and compare against the baseline that has now passed five times:

```
5 rows, all offsetHeight 79
.project-row  grid 237.891px 321.164px 117.852px · padding 12px 0px · minHeight 60px
.project-row__name  Fraunces 20px 600 · fontVariantCaps normal
.project-row__path  IBM Plex Mono 13px · nowrap + ellipsis
.pill  bg rgb(36,31,26) · color rgb(179,166,145) · Atkinson · radius 6px
.project-row__action .btn  IBM Plex Mono 12px · rgb(219,112,72) · transparent
row viewTransitionName: none
```

**Compare `offsetHeight` to `offsetHeight`** — `getBoundingClientRect` reports 78.5 for these same rows, and that difference has been mistaken for a regression before.

The app pins `data-theme` at load from `localStorage["bridge.appearance"]`, so to switch theme set that value and RELOAD; emulating `prefers-color-scheme` alone is silently ignored. **Mit's Arc value is `"light"` — restore it when done.**

- [ ] **Step 5: Record the outcome** in `.superpowers/sdd/2026-08-03-bridge-almanac-projects/progress.md`: what was measured in Arc, what the cascade gate showed, and anything that did NOT work. If the swap does not visibly improve the jolt for Mit, **say so plainly** rather than declaring victory — three prior sessions each reported a fix that had changed nothing for him.

- [ ] **Step 6: Commit the ledger update.**

---

## Post-plan, deliberately NOT in scope

- Deleting the now-inert `@view-transition` rules and `tests/test_view_transitions.py`. Do this only after Task 11 passes, and note that same-document view transitions become available at that point — the right place to add motion back.
- Resolving the speculation-rules prefetch (`base.html:82-89`), which now duplicates the router's fetch. Measure first; it may still win the first navigation.
- `/project/{id}` and form posts.
- Two pre-existing bugs, pinned by tests so they are not misread as regressions: `schedule.js:87` queries `[data-scheduled-count]`, an attribute in no template, so `bumpScheduledCount` is already a no-op; `launch.js:10` and `schedule.js:8` both define a global `announce`, with schedule.js winning by load order.

## Self-review

**Spec coverage:** §3 seam → Tasks 8, 9. §4 init contract → Tasks 1, 3–7. §5 layer 1 → Task 2; layer 2 → Task 8; layer 3 → Tasks 1, 4–7, 10. §6 router → Task 9 (all six numbered behaviours: interception 1, leave/fetch 2, replace 3, history 4, enter 5, focus 6). §7 consequences → Post-plan section. §8 verification → Task 11. No gaps.

**Type consistency:** `bridgePage.onEnter/onLeave/enter/leave` (Task 1) used identically in 3–7, 10. `window.bridgeNavigate` produced in Task 9, consumed in Task 5 behind an `else` guard so ordering is safe. `_layout_for` (Task 8) named consistently. `paintScheduledTimes(root)` keeps its existing signature. `savePrompt(field)` introduced in Task 6 and used by both call sites there.

**Known ordering note:** Task 5 references `window.bridgeNavigate` before Task 9 creates it. This is deliberate and safe — the call is guarded by `if (window.bridgeNavigate) ... else window.location.assign(...)`, so the intermediate commits behave exactly as they do today.
