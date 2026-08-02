// Live updates, patched into specific leaf nodes.
//
// This file deliberately cannot see the handoff <textarea>. A reload, or an
// innerHTML swap over a card, would discard an in-progress prompt edit -- the
// one piece of state Bridge cannot rebuild from transcripts. So: textContent on
// leaf nodes, nothing structural, no reload, ever.
//
// The wire has three named events. `snapshot` is the full picture and arrives
// on every connect (which is why there is no Last-Event-ID replay to write:
// a reconnect is already complete). `delta` carries only what changed, plus
// `removed` -- the tombstone that lets a card stop claiming a session that has
// ended. `refresh` means resync over REST.

const LIVE_BAND = "[data-live-path]";
const LIVE_STATES = ["busy", "working", "idle", "waiting", "unknown", "ended"];

// A connection is only "healthy" once it has proved itself. Resetting the
// backoff the moment onopen fires turns an accept-then-close server into a hot
// reconnect loop, so the counter is cleared on evidence, not on optimism.
const HEALTHY_FRAMES = 2;
const HEALTHY_MS = 1000;

function bandFor(path) {
  // CSS.escape: project paths contain slashes, dots and spaces.
  return document.querySelector(`[data-live-path="${CSS.escape(path)}"]`);
}

function setBandState(band, status) {
  const state = LIVE_STATES.includes(status) ? status : "unknown";
  band.classList.remove(...LIVE_STATES.map((name) => `live--${name}`));
  band.classList.add(`live--${state}`);
}

function applyLive(live) {
  for (const [path, state] of Object.entries(live || {})) {
    const band = bandFor(path);
    // Update only the existing leaf and its state class. The surrounding card,
    // including any in-progress handoff textarea, retains its identity.
    if (band) setBandState(band, state.status);
    if (band) band.textContent = state.status;
  }
}

function applyRemoved(removed) {
  for (const path of removed || []) {
    const band = bandFor(path);
    // The session is gone. Without this the card keeps its live band until the
    // page is reloaded, which is the thing the tombstone exists to prevent.
    if (band) setBandState(band, "ended");
    if (band) band.textContent = "ended";
  }
}

// Backoff for OUR reconnects (the ones after a capped stream). EventSource's
// own reconnect handles the rest.
const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 30000;
let backoffMs = BACKOFF_MIN_MS;

function healthy(frames, openedAt) {
  // Conductor's gate: two good frames, or one and a second of uptime. Proof,
  // not optimism -- resetting on `onopen` alone turns an accept-then-close
  // server into a hot reconnect loop.
  return frames >= HEALTHY_FRAMES
    || (frames >= 1 && Date.now() - openedAt >= HEALTHY_MS);
}

function connect() {
  const source = new EventSource("/events");
  const openedAt = Date.now();
  let frames = 0;

  const handle = (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (error) {
      // A bad frame is skipped; the stream keeps going. Letting it throw would
      // kill live updates for the rest of the session over one bad payload.
      console.error("bridge: malformed live payload", error);
      return;
    }
    frames += 1;
    if (healthy(frames, openedAt)) backoffMs = BACKOFF_MIN_MS;
    applyLive(payload.live);
    applyRemoved(payload.removed);
  };

  source.addEventListener("snapshot", handle);
  source.addEventListener("delta", handle);
  source.addEventListener("refresh", () => {
    // The server capped the stream. Reconnecting gets a complete snapshot, so
    // there is nothing to replay and nothing to reload. The delay is zero when
    // the connection had proved itself, and backs off when it had not.
    source.close();
    const delay = healthy(frames, openedAt) ? 0 : backoffMs;
    if (!healthy(frames, openedAt)) {
      backoffMs = Math.min(backoffMs * 2, BACKOFF_MAX_MS);
    }
    window.setTimeout(connect, delay);
  });

  return source;
}

const liveSource = connect();

window.bridgeLiveSource = liveSource;
