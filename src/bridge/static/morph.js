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
    // Every child NODE, not just element children -- `<p>Handoff saved
    // <time>...</time></p>` has a text node before `<time>`, and cloning only
    // `.children` (the old approach) silently dropped it.
    for (const kid of Array.from(node.childNodes)) {
      el.append(kid.nodeType === 3 ? document.createTextNode(kid.textContent) : cloneInto(kid));
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
    if (reconcileChildren(live, incoming, opts)) changed = true;
    if (changed) opts.onChange(live);
  }

  // Reconciles every CHILD NODE -- text included, not just element children.
  // A leaf (`<span>text</span>`) and a mixed node (`<p>text<time>...</time>
  // </p>`) are the same case here: both are just a childNodes sequence that
  // may contain text entries, elements, or both, in any order. Element
  // matching (keyed, then unkeyed-by-tag) is unchanged from before; text has
  // no key or tag to match on, so it is matched purely by POSITION among the
  // other text nodes -- the same "next one of this kind" rule the unkeyed
  // element cursor already used, generalised to a second kind.
  function reconcileChildren(live, incoming, opts) {
    const liveByKey = new Map();
    for (const el of Array.from(live.children)) {
      const k = opts.key(el);
      if (k != null) liveByKey.set(k, el);
    }
    const unkeyedEls = Array.from(live.children).filter((el) => opts.key(el) == null);
    const liveTexts = Array.from(live.childNodes).filter((n) => n.nodeType === 3);
    let unkeyedCursor = 0;
    let textCursor = 0;
    let changed = false;
    const desired = [];
    for (const inc of Array.from(incoming.childNodes)) {
      let node = null;
      if (inc.nodeType === 3) {
        if (textCursor < liveTexts.length) {
          node = liveTexts[textCursor];
          textCursor += 1;
          if (node.textContent !== inc.textContent) {
            node.textContent = inc.textContent;
            changed = true;
          }
        } else {
          node = document.createTextNode(inc.textContent);
          changed = true;
        }
      } else {
        const k = opts.key(inc);
        if (k != null && liveByKey.has(k)) {
          node = liveByKey.get(k);
        } else if (k == null) {
          while (unkeyedCursor < unkeyedEls.length &&
                 (unkeyedEls[unkeyedCursor].localName || unkeyedEls[unkeyedCursor].tag) !==
                 (inc.localName || inc.tag)) unkeyedCursor += 1;
          if (unkeyedCursor < unkeyedEls.length) { node = unkeyedEls[unkeyedCursor]; unkeyedCursor += 1; }
        }
        if (node) {
          morphNode(node, inc, opts);
        } else {
          node = cloneInto(inc);
          opts.onChange(node);
          changed = true;
        }
      }
      desired.push(node);
    }
    // Remove whatever the server dropped -- element or text, but never a
    // protected node. Every reused or newly-placed node is already in
    // `desired`, so anything left out of it is exactly what to drop.
    for (const el of Array.from(live.childNodes)) {
      if (desired.indexOf(el) !== -1 || opts.ignore(el)) continue;
      el.remove();
      changed = true;
    }
    // Put the desired sequence in order; insertBefore moves existing nodes.
    for (let i = 0; i < desired.length; i += 1) {
      if (live.childNodes[i] !== desired[i]) {
        live.insertBefore(desired[i], live.childNodes[i] || null);
        changed = true;
      }
    }
    return changed;
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
