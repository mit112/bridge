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
               (unkeyed[unkeyedCursor].localName || unkeyed[unkeyedCursor].tag) !==
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
