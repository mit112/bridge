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
    fetch(window.location.href, { headers: { "X-Bridge-Fragment": "1" }, credentials: "same-origin" })
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
    // No baseline yet for this view (freshest possible reading is this frame
    // itself) -- adopt it rather than comparing against null, which would
    // otherwise never let a first-ever frame establish a baseline to bump from.
    if (baselineGeneration == null) { baselineGeneration = generation; return; }
    if (Number.isFinite(generation) && generation > baselineGeneration) {
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
