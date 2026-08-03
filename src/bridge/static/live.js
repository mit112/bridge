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

function totalNode(name) {
  const node = query(`[data-dashboard-total="${cssValue(name)}"]`);
  if (!node) return null;
  // The scheduled total retains its older hook on the <dd> for schedule.js.
  return node.querySelector ? (node.querySelector("[data-topbar-scheduled]") || node) : node;
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

function patchGit(card, git) {
  if (!card || !git) return;
  const branch = leaf(card, "[data-git-branch]");
  const dirty = leaf(card, "[data-git-dirty]");
  const ahead = leaf(card, "[data-git-ahead]");
  const stale = leaf(card, "[data-git-stale]");
  const cache = leaf(card, "[data-git-cache]");
  const notRepo = leaf(card, "[data-git-status=\"not_a_repo\"]");
  const unavailable = leaf(card, "[data-git-status=\"unavailable\"]");
  const ok = git.status === "ok";
  setText(branch, ok ? git.branch : "");
  setHidden(branch, !ok);
  setText(dirty, git.dirty_count ? ` · ${git.dirty_count} dirty` : "");
  setHidden(dirty, !ok || !git.dirty_count);
  setText(ahead, git.ahead ? ` · ${git.ahead} ahead` : "");
  setHidden(ahead, !ok || !git.ahead);
  setText(stale, git.stale ? `⚠ uncommitted for ${git.oldest_uncommitted_at || ""}` : "");
  setHidden(stale, !git.stale);
  setText(cache, git.cached_at ? ` · as of ${git.cached_at}` : "");
  setHidden(cache, !git.cached_at);
  setHidden(notRepo, git.status !== "not_a_repo");
  setHidden(unavailable, git.status !== "unavailable");
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
    patchGit(card, update.git);
    patchBurn(card, update.burn);
  }
}

function applyCardOrder(order) {
  if (!Array.isArray(order) || !document.querySelectorAll) return;
  const list = query("[data-cards-list]");
  if (!list) return;
  const nodes = [...document.querySelectorAll("[data-project-card]")];
  const byId = new Map(nodes.map((node) => [String(node.getAttribute("data-project-card")), node]));
  const incoming = order.map(String);
  const existing = nodes.map((node) => String(node.getAttribute("data-project-card")));
  const sameSet = incoming.length === existing.length
    && incoming.every((id) => byId.has(id));
  const status = query("[data-project-membership-status]");
  setHidden(status, sameSet);
  if (!sameSet) setText(status, "Project list changed - reopen the panel to update cards.");
  // append() moves an existing node; it does not clone, create, or replace it.
  for (const id of incoming) {
    const node = byId.get(id);
    if (node) list.append(node);
  }
}

let lastGeneration = null;
let lastIndexAt = null;
let lastConnectionState = null;
let transportReconnecting = false;

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

function announceConnectionState(state) {
  if (state === lastConnectionState) return;
  lastConnectionState = state;
  const strip = query("[data-freshness-strip]");
  const label = query("[data-freshness-label]");
  if (strip) {
    strip.setAttribute("data-freshness-state", state);
    strip.setAttribute("data-server", state === "unavailable" ? "unavailable" : "available");
  }
  setText(label, state);
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
  for (const name of ["projects", "running", "queued", "scheduled"]) {
    if (topbar[name] != null) setText(totalNode(name), topbar[name]);
  }
  if (topbar.today != null) setText(totalNode("today"), formatKilo(topbar.today));
  if (topbar.last_5h != null) setText(totalNode("last_5h"), formatKilo(topbar.last_5h));
  if (topbar.burn_rate != null) setText(totalNode("burn_rate"), `${formatKilo(topbar.burn_rate)}/h`);
  if (topbar.last_index != null) setText(totalNode("last_index"), topbar.last_index);
  if (update.cards) applyCardUpdates(update.cards);
  if (update.card_order) applyCardOrder(update.card_order);
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

lastIndexAt = initialIndexAt();
const initialStrip = query("[data-freshness-strip]");
if (initialStrip) {
  // `getAttribute` returns `null` for a missing attribute (Overview's strip
  // never renders `data-generation`), and `Number(null)` is 0 -- a real,
  // finite generation, not the "unknown" `patchFreshness` needs it to mean.
  // Reading the raw value first keeps "absent" and "present as 0" distinct.
  const rawGeneration = initialStrip.getAttribute("data-generation");
  const initialGeneration = rawGeneration == null ? NaN : Number(rawGeneration);
  lastGeneration = Number.isFinite(initialGeneration) ? initialGeneration : null;
  announceConnectionState(connectionState(initialStrip.getAttribute("data-server"), Math.floor(Date.now() / 1000)));
}

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
