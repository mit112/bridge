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
  // `.id` reflects the `id` content attribute in every real DOM; launch.js
  // reads `field.id` (not `getAttribute("id")`) to build a status selector, and
  // nothing exercised that path until this task's tests, which is how this gap
  // stayed unnoticed.
  get id() { return this.getAttribute("id") || ""; }
  set id(v) { this.setAttribute("id", v); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  // Every real DOM keeps `classList` live against the `class` content
  // attribute in both directions. `classList` was previously a Set snapshotted
  // only at construction time, so a class assigned via `setAttribute("class",
  // ...)` after creation -- exactly what a query selector like `.shell__body`
  // is checked against -- was invisible to every class selector forever.
  setAttribute(name, value) {
    this.attrs.set(name, String(value));
    if (name === "class") {
      this.classList = new Set(String(value).split(/\s+/).filter(Boolean));
    }
  }
  removeAttribute(name) { this.attrs.delete(name); }
  hasAttribute(name) { return this.attrs.has(name); }
  getAttributeNames() { return [...this.attrs.keys()]; }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.children = []; }
  append(child) { child.parent = this; this.children.push(child); }
  remove() {
    if (!this.parent) return;
    const i = this.parent.children.indexOf(this);
    if (i >= 0) this.parent.children.splice(i, 1);
    this.parent = null;
  }
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
  // Every static file here is `<script defer>`, and per spec the parser sets
  // readiness to "interactive" BEFORE deferred scripts run -- a `defer`
  // script never observes "loading" at evaluation time. Defaulting to
  // "interactive" keeps this harness from presenting a state real page loads
  // never produce (a bug that cost real time: shell.js's first-view
  // bootstrap once branched on `readyState === "loading"` and a test that
  // fabricated exactly that value stayed green over the resulting
  // regression).
  doc.readyState = "interactive";
  globalThis.document = doc;
  globalThis.window = globalThis;
  globalThis.window.matchMedia = () => ({ matches: false, addEventListener() {} });
  // Task 9 (router.js): `window.addEventListener("popstate", ...)` runs at
  // module load, and `navigate()` reads `window.location` and
  // `window.history` -- none of which a bare `globalThis` has. Minimal stubs,
  // matching this file's existing style of counting what the duplicate-hazard
  // tests assert on (`__calls.locationAssign` below).
  const globalListeners = new Map();
  globalThis.addEventListener = (type, fn) => {
    if (!globalListeners.has(type)) globalListeners.set(type, []);
    globalListeners.get(type).push(fn);
  };
  globalThis.dispatchEvent = (event) => {
    for (const fn of globalListeners.get(event.type) || []) fn(event);
  };
  globalThis.location = {
    href: "http://localhost/",
    origin: "http://localhost",
    // A getter, not a static string: router.js reads `window.location.pathname`
    // to tell a same-document hash link (the skip-link's `#main`) apart from a
    // real navigation, so it must track `href` rather than go stale against it.
    get pathname() { return new URL(this.href).pathname; },
    assign(href) { globalThis.__calls.locationAssign = href; },
  };
  globalThis.history = { pushState() {} };
  globalThis.localStorage = {
    _m: new Map(),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
    removeItem(k) { this._m.delete(k); },
  };
  globalThis.sessionStorage = {
    _m: new Map(),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
    removeItem(k) { this._m.delete(k); },
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
