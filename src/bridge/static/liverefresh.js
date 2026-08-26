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
  const LIVE_UNSET = Symbol("unset");   // no fast-signal baseline observed yet for this view

  let owned = false;
  let projectId = null;
  let baselineGeneration = null;   // last generation acknowledged for this view
  let lastSeenGeneration = null;   // most recent generation observed on the wire
  let pendingGeneration = null;    // a bump waiting to be applied
  let lastLiveSignal = LIVE_UNSET; // last ~3s fast signal (per-card status or topbar.running) observed
  let refreshRequested = false;    // a trigger (generation bump or live-signal change) fired
  let refreshVersion = 0;         // bumped alongside refreshRequested so a fetch in flight
                                  // doesn't clear a request that arrived after it started
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
    if (el === document.activeElement) return true;
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
    if (!refreshRequested) return;
    if (protectedFocus()) return;                 // defer: retried on the next frame,
                                                    // refreshRequested stays set
    const versionAtFetch = refreshVersion;
    const generationAtFetch = lastSeenGeneration;
    // Pathname AND query: /schedule?view=upcoming and /schedule?view=history
    // share a pathname, so a pathname-only check would let an in-flight fetch
    // for one view morph its stale fragment over the other the instant the
    // user swaps views while this fetch is in flight.
    const pathAtFetch = window.location.pathname + window.location.search;
    fetch(window.location.href, { headers: { "X-Bridge-Fragment": "1" }, credentials: "same-origin" })
      .then((response) => {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.text();
      })
      .then((html) => {
        // The route can change while this fetch is in flight (leave() fires,
        // a new enter() runs) -- morphing a stale fragment into whatever page
        // is now on screen, or stamping this fetch's generation onto the new
        // view's baseline, would both be silent corruption. Bail and keep the
        // DOM/baseline exactly as the new view already set them.
        if (!owned || window.location.pathname + window.location.search !== pathAtFetch) return;
        const parsed = window.bridgeFragment.parse(html);
        if (!parsed || !parsed.body) throw new Error("unusable fragment");
        const liveBody = document.querySelector(".shell__body");
        if (!liveBody) return;
        window.bridgeMorph(liveBody, parsed.body, { ignore: ignoreNode, onChange: highlight });
        // Re-apply client-derived display the morph just reverted (e.g. localized
        // schedule clocks): the enter() pass does NOT re-run on an in-place morph.
        if (window.bridgePage && window.bridgePage.morphed) window.bridgePage.morphed();
        baselineGeneration = generationAtFetch;
        if (pendingGeneration != null && pendingGeneration <= generationAtFetch) pendingGeneration = null;
        // A newer trigger (generation bump or live-signal change) may have
        // fired while this fetch was in flight -- refreshVersion moved on,
        // so leave refreshRequested set rather than dropping that trigger.
        if (refreshVersion === versionAtFetch) refreshRequested = false;
      })
      .catch((error) => { console.error("bridge: live refresh kept stale DOM", error); });
  }

  // The route's fast (~3s) live signal for the CURRENT view only -- a
  // workspace watches just its own project's card, never another project's;
  // /schedule and /diagnostics watch the shared topbar running count;
  // /settings has no live data and always reads as null (never changes).
  function currentLiveSignal(frame) {
    const path = currentPath();
    if (WORKSPACE.test(path)) {
      const status = frame && frame.cards && frame.cards[projectId] &&
        frame.cards[projectId].live && frame.cards[projectId].live.status;
      return status == null ? null : status;
    }
    if (path === "/schedule" || path === "/diagnostics") {
      const running = frame && frame.topbar && frame.topbar.running;
      return running == null ? null : running;
    }
    return null;
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
    // A non-finite generation can't serve as a baseline either: NaN fails
    // every `>` comparison, which would silently wedge refresh off until the
    // next enter() rather than just waiting for a usable frame.
    if (baselineGeneration == null) {
      if (Number.isFinite(generation)) baselineGeneration = generation;
    } else if (Number.isFinite(generation) && generation > baselineGeneration) {
      pendingGeneration = generation;
      refreshRequested = true;
      refreshVersion += 1;
      schedule();
    }

    // Fast per-route live signal, same cold-start discipline as generation:
    // the first frame for this view only adopts a baseline, never triggers.
    const signal = currentLiveSignal(frame);
    if (lastLiveSignal === LIVE_UNSET) {
      lastLiveSignal = signal;
    } else if (signal !== lastLiveSignal) {
      lastLiveSignal = signal;
      refreshRequested = true;
      refreshVersion += 1;
      schedule();
    }
  }

  function enter() {
    const path = currentPath();
    owned = isOwned(path);
    projectId = projectIdOf(path);
    baselineGeneration = lastSeenGeneration;       // only future bumps refresh
    pendingGeneration = null;
    lastLiveSignal = LIVE_UNSET;
    refreshRequested = false;
  }

  function leave() {
    if (timer) { clearTimeout(timer); timer = null; }
    owned = false;
    pendingGeneration = null;
    refreshRequested = false;
  }

  window.bridgeLive.onFrame(onFrame);
  window.bridgePage.onEnter(enter);
  window.bridgePage.onLeave(leave);

  window.bridgeLiveRefresh = { _onFrame: onFrame, _refreshNow: refreshNow };
})();
