// Live dashboard updates. Only existing leaves are patched: cards and handoff
// textareas are server-rendered identity boundaries and are never replaced.

const LIVE_STATES = ["busy", "working", "idle", "waiting", "unknown", "ended"];
const CONNECTION_STATES = ["connected", "reconnecting", "stale", "unavailable"];
const INDEX_STALE_SECONDS = 45;

function cssValue(value) {
  return typeof CSS !== "undefined" && CSS.escape ? CSS.escape(String(value)) : String(value);
}

function query(selector) {
  return document.querySelector(selector);
}

function setText(node, value) {
  if (node) node.textContent = value == null ? "" : String(value);
}

function setHidden(node, hidden) {
  if (!node) return;
  node.hidden = Boolean(hidden);
  if (hidden) node.setAttribute("hidden", "");
  else node.removeAttribute("hidden");
}

function formatKilo(value) {
  const number = Number(value || 0);
  if (number < 1000) return String(number);
  if (number < 1000000) return `${Math.round(number / 1000)}k`;
  return `${(number / 1000000).toFixed(1)}M`;
}

// Plural on purpose. The Overview renders each total TWICE -- once in the
// command strip that is actually on screen, once in the metrics list inside a
// collapsed <details> -- and a single-node lookup patched whichever came first
// in the document, so the visible number froze at its page-load value while
// its hidden twin ticked along correctly.
function totalNodes(name) {
  const selector = `[data-dashboard-total="${cssValue(name)}"]`;
  const nodes = document.querySelectorAll ? [...document.querySelectorAll(selector)] : [];
  // The scheduled total retains its older hook on the <dd> for schedule.js.
  return nodes.map((node) => (node.querySelector
    ? node.querySelector("[data-topbar-scheduled]") || node
    : node));
}

// A command cell colours its number from that number's own value, and names
// the class it uses in `data-cell-flag`. Writing the text without the class
// leaves a cell lit for a count that has since patched down to zero -- the
// same "colour disagrees with the word" failure the attention pill had. The
// attribute keeps the class name in the template that chose it.
function setTotal(name, value) {
  for (const node of totalNodes(name)) {
    setText(node, value);
    const cell = node.closest ? node.closest("[data-cell-flag]") : null;
    const flag = cell ? cell.getAttribute("data-cell-flag") : null;
    if (flag && cell.classList && cell.classList.toggle) {
      cell.classList.toggle(flag, Number(value) > 0);
    }
  }
}

function bandFor(path) {
  return query(`[data-live-path="${cssValue(path)}"]`);
}

function setBandState(band, status) {
  if (!band) return;
  const state = LIVE_STATES.includes(status) ? status : "unknown";
  const parent = band.closest ? band.closest("[data-live-parent]") : band.parentNode;
  const target = parent && parent.classList ? parent : band;
  if (target.classList && target.classList.remove) {
    target.classList.remove(...LIVE_STATES.map((name) => `live--${name}`));
    target.classList.add(`live--${state}`);
  }
}

function applyLegacyLive(live) {
  for (const [path, state] of Object.entries(live || {})) {
    const band = bandFor(path);
    setBandState(band, state.status);
    // Kept as a leaf-only compatibility path for the pre-schema tombstone
    // tests. It cannot reach a card subtree or a handoff textarea.
    if (band) band.textContent = state.status;
  }
}

function applyRemoved(removed) {
  for (const path of removed || []) {
    const band = bandFor(path);
    setBandState(band, "ended");
    if (band) band.textContent = "ended";
  }
}

function cardFor(projectId) {
  return query(`[data-project-card="${cssValue(projectId)}"]`);
}

// Overview's leaf-light DOM has no per-project git/burn/sparkline leaves (and
// the workspace renders git as static text, never as these hooks), so every
// lookup below the card must tolerate a null result -- not just a card that
// itself does not exist.
function leaf(card, selector) {
  return card && card.querySelector ? card.querySelector(selector) : null;
}

function patchLive(card, live) {
  if (!card || !live) return;
  const status = live.status || (live.available ? "unknown" : "ended");
  const band = leaf(card, "[data-live-status]");
  setBandState(band, status);
  setText(band, status);
  setText(leaf(card, "[data-live-age]"), live.started_at == null ? "" : `· ${live.started_at}`);
  setText(leaf(card, "[data-live-model]"), live.model ? `· ${live.model}` : "");
  setText(leaf(card, "[data-live-effort]"), live.effort ? `/${live.effort}` : "");
}

function patchBurn(card, burn) {
  if (!card || !burn) return;
  setText(leaf(card, "[data-burn-today]"), `${formatKilo(burn.today)} today`);
  setText(leaf(card, "[data-burn-last-5h]"), `${formatKilo(burn.last_5h)} last 5h`);
  const line = leaf(card, "[data-sparkline]");
  if (line) line.setAttribute("points", burn.spark_points || "");
}

function applyCardUpdates(cards) {
  for (const [projectId, update] of Object.entries(cards || {})) {
    const card = cardFor(projectId);
    if (!card) continue;
    patchLive(card, update.live);
    patchBurn(card, update.burn);
  }
}

let lastGeneration = null;
let lastIndexAt = null;
let lastConnectionState = null;
let transportReconnecting = false;

// Frame fan-out: liverefresh.js subscribes here rather than opening a second
// EventSource. Additive -- Overview patching below is unchanged.
const frameListeners = [];
function emitFrame(payload) {
  for (const fn of frameListeners) {
    try { fn(payload); } catch (error) { console.error("bridge: frame listener failed", error); }
  }
}

function initialIndexAt() {
  const strip = query("[data-freshness-strip]");
  if (!strip) return null;
  const value = Number(strip.getAttribute("data-index-at"));
  return Number.isFinite(value) && value > 0 ? value : null;
}

function indexedAge(nowSeconds) {
  if (lastIndexAt == null) return null;
  return Math.max(0, nowSeconds - lastIndexAt);
}

function connectionState(server, nowSeconds) {
  if (server === "unavailable" || lastIndexAt == null) return "unavailable";
  if (indexedAge(nowSeconds) >= INDEX_STALE_SECONDS) return "stale";
  if (transportReconnecting) return "reconnecting";
  return "connected";
}

// Sentence case, matching `_shell.html`'s `{{ state | capitalize }}`. The state
// itself stays lowercase everywhere it is machine-read (the attribute below,
// CONNECTION_STATES, CSS); this is the visible word only.
function stateLabel(state) {
  const text = String(state || "");
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}

function announceConnectionState(state) {
  if (state === lastConnectionState) return;
  lastConnectionState = state;
  const strip = query("[data-freshness-strip]");
  const label = query("[data-freshness-label]");
  if (strip) {
    strip.setAttribute("data-freshness-state", state);
    strip.setAttribute("data-server", state === "unavailable" ? "unavailable" : "available");
  }
  setText(label, stateLabel(state));
}

function patchFreshness(update) {
  const freshness = update.freshness;
  if (!freshness) return;
  const generation = Number(update.generation);
  // `lastGeneration == null` means the boot read found no usable baseline
  // (Overview's freshness strip never carries `data-generation` -- see the
  // boot IIFE below). With no baseline to compare against, the first update
  // of either kind is accepted rather than rejected as "not newer".
  const successfulGeneration = update.kind === "snapshot"
    || lastGeneration == null
    || (Number.isFinite(generation) && generation > lastGeneration);
  if (successfulGeneration && freshness.index_at != null) {
    lastIndexAt = Number(freshness.index_at);
    lastGeneration = Number.isFinite(generation) ? generation : lastGeneration;
  } else if (lastGeneration == null && update.kind === "snapshot") {
    lastGeneration = Number.isFinite(generation) ? generation : 0;
  }
  const strip = query("[data-freshness-strip]");
  if (strip) {
    if (update.generated_at != null) strip.setAttribute("data-generated-at", update.generated_at);
    if (Number.isFinite(generation)) strip.setAttribute("data-generation", generation);
    if (freshness.index_at != null) strip.setAttribute("data-index-at", freshness.index_at);
    strip.setAttribute("data-server", freshness.server || "unavailable");
  }
  const age = indexedAge(Math.floor(Date.now() / 1000));
  setText(query("[data-freshness-age]"), age == null ? "never indexed" : `${age}s ago`);
  announceConnectionState(connectionState(freshness.server, Math.floor(Date.now() / 1000)));
}

function applyDashboardUpdate(update) {
  if (!update || update.schema !== 1 || (update.kind !== "snapshot" && update.kind !== "patch")) return false;
  const topbar = update.topbar || {};
  // `dirty` and `attention` are command-strip-only; they have no twin in the
  // metrics list, and before the strip carried hooks they were on the wire
  // with nothing reading them.
  for (const name of ["projects", "running", "queued", "scheduled", "dirty", "attention"]) {
    if (topbar[name] != null) setTotal(name, topbar[name]);
  }
  if (topbar.today != null) setTotal("today", formatKilo(topbar.today));
  if (topbar.last_5h != null) setTotal("last_5h", formatKilo(topbar.last_5h));
  if (topbar.burn_rate != null) setTotal("burn_rate", `${formatKilo(topbar.burn_rate)}/h`);
  // `last_index` is deliberately NOT patched here: the server renders it as a
  // human "Xm ago" (Jinja `ago_epoch`), but `topbar.last_index` on the wire is
  // a raw epoch, so writing it would replace "3m ago" with "1785754250" on the
  // first tick. Index time changes rarely, so it stays server-rendered until a
  // reload rather than being reformatted client-side.
  if (update.cards) applyCardUpdates(update.cards);
  if (update.diagnostics && update.diagnostics.alert != null) {
    setHidden(query("[data-diagnostics-alert]"), !update.diagnostics.alert);
  }
  patchFreshness(update);
  return true;
}

// Connection/backoff handling remains transport-only. Heartbeat comments never
// reach this function and therefore never announce or reset indexed freshness.
const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 30000;
let backoffMs = BACKOFF_MIN_MS;
const HEALTHY_FRAMES = 2;
const HEALTHY_MS = 1000;

function healthy(frames, openedAt) {
  return frames >= HEALTHY_FRAMES || (frames >= 1 && Date.now() - openedAt >= HEALTHY_MS);
}

function refreshDashboard(button) {
  const status = query("[data-refresh-status]");
  if (button) button.disabled = true;
  setText(status, "Refreshing...");
  return fetch("/api/refresh", { method: "POST" })
    .then((response) => response.json().then((body) => ({ response, body })))
    .then(({ response, body }) => {
      if (!response.ok || !applyDashboardUpdate(body)) throw new Error("refresh failed");
      // A failed reindex still comes back as an HTTP-success envelope --
      // `{refresh: {completed: false, error: "..."}}` -- carrying whatever
      // data was already on hand. `response.ok` and `applyDashboardUpdate`
      // both pass on that envelope, so this is the one place left that can
      // tell "refreshed" from "tried and failed, kept the old data".
      if (body.refresh && body.refresh.attempted && !body.refresh.completed) {
        throw new Error(body.refresh.error || "refresh failed");
      }
      setText(status, "Updated");
    })
    .catch(() => setText(status, "Refresh failed; existing data kept."))
    .finally(() => { if (button) button.disabled = false; });
}

if (document.addEventListener) {
  document.addEventListener("click", (event) => {
    const button = event.target && event.target.closest
      ? event.target.closest("[data-dashboard-refresh]") : null;
    if (button) refreshDashboard(button);
  });
}

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

function connect() {
  const source = new EventSource("/events");
  const openedAt = Date.now();
  let frames = 0;
  source.onopen = () => { transportReconnecting = false; };
  source.onerror = () => {
    transportReconnecting = true;
    announceConnectionState(connectionState("available", Math.floor(Date.now() / 1000)));
  };

  const handle = (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (error) {
      console.error("bridge: malformed live payload", error);
      return;
    }
    frames += 1;
    emitFrame(payload);
    if (healthy(frames, openedAt)) backoffMs = BACKOFF_MIN_MS;
    if (payload.schema === 1) applyDashboardUpdate(payload);
    else {
      // Compatibility with the pre-schema event shape. New server frames never
      // take this branch, but retaining it makes old clients/tests fail safe.
      applyLegacyLive(payload.live);
      applyRemoved(payload.removed);
    }
  };

  source.addEventListener("snapshot", handle);
  source.addEventListener("update", handle);
  source.addEventListener("delta", handle);
  source.addEventListener("refresh", () => {
    source.close();
    const delay = healthy(frames, openedAt) ? 0 : backoffMs;
    if (!healthy(frames, openedAt)) backoffMs = Math.min(backoffMs * 2, BACKOFF_MAX_MS);
    window.setTimeout(connect, delay);
  });
  return source;
}

const liveSource = connect();
if (typeof setInterval === "function") {
  const ageTimer = setInterval(() => {
    const age = indexedAge(Math.floor(Date.now() / 1000));
    setText(query("[data-freshness-age]"), age == null ? "never indexed" : `${age}s ago`);
    const strip = query("[data-freshness-strip]");
    if (strip) announceConnectionState(connectionState(strip.getAttribute("data-server"), Math.floor(Date.now() / 1000)));
  }, 1000);
  if (ageTimer && ageTimer.unref) ageTimer.unref();
}

window.bridgeApplyDashboardUpdate = applyDashboardUpdate;
window.bridgeLiveSource = liveSource;
window.bridgeLive = { onFrame(fn) { frameListeners.push(fn); }, _emit: emitFrame };
