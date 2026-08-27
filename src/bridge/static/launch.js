// The launch band: press ▶ to spawn the session, and persist prompt edits.
//
// Both listeners are delegated from the document, matching copy.js, so one
// listener serves every card. The clipboard helper is copy.js's — a launch
// failure has to hand the prompt over, and re-implementing the fallback would
// give the same action two behaviours.

const LAUNCHING = "Launching…";

function announce(selector, message) {
  const status = document.querySelector(selector);
  if (status) status.textContent = message;
}

// Both Run now affordances use this one reader. The select values remain the
// sole launch authority and are read fresh for every click.
window.bridgeLaunchBody = function bridgeLaunchBody(id, projectPath) {
  const model = document.querySelector(`[data-launch-model="${id}"]`);
  const effort = document.querySelector(`[data-launch-effort="${id}"]`);
  const perm = document.querySelector(`[data-launch-perm="${id}"]`);
  return {
    project_path: projectPath,
    mode: "terminal",
    model: model ? model.value : null,
    effort: effort ? effort.value : null,
    // Read fresh from the select on every click and never cached: the server
    // holds no permission memory, so this is the only thing that decides the
    // mode, and it must not be able to carry over from a previous launch.
    permission_mode: perm ? perm.value : null,
  };
};

// Tracks, per field, the sequence number of the most recently ISSUED save --
// not the most recently RESOLVED one. `focusout` and the `onLeave` flush can
// both fire a save for the same field, so two requests can be in flight at
// once; the network makes no promise about which answers first.
const promptSaveSeq = new WeakMap();

// Save an edited prompt when focus leaves the field, and only when the text
// actually changed — `focusout` (which bubbles, unlike `blur`) fires on every
// tab-through, and a PATCH per tab-through would re-journal an unchanged prompt.
async function savePrompt(field) {
  const handoffId = field.getAttribute("data-prompt-handoff");
  const saved = field.dataset.savedPrompt ?? field.defaultValue;
  if (field.value === saved) return;

  // Captured now, not re-read after the `await` below: the user can keep
  // typing while this request is in flight, and `savedPrompt` must record
  // what the server actually received, never whatever happens to be in the
  // field when the response lands.
  const submitted = field.value;
  const seq = (promptSaveSeq.get(field) || 0) + 1;
  promptSaveSeq.set(field, seq);

  const key = `[data-prompt-status="${field.id}"]`;
  try {
    const response = await fetch(`/api/handoff/${encodeURIComponent(handoffId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ next_prompt: submitted }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    // An older save that resolves after a newer one must not overwrite the
    // newer save's already-recorded result -- only the request that is still
    // the newest ISSUED for this field may record its own success.
    if (promptSaveSeq.get(field) === seq) {
      field.dataset.savedPrompt = submitted;
      clearHandoffDraft(handoffId);
    }
    announce(key, "✓ Prompt saved");
  } catch (error) {
    // The prompt is the one thing Bridge cannot rebuild from transcripts, so a
    // failed save says so in words and points at the way out. It is also the
    // only copy of the edit: a navigation right after this still swaps the
    // field away (the router only waits for this promise to SETTLE, not
    // succeed), so the failed text is persisted here and restored on the next
    // `onEnter` rather than left to vanish with the old field.
    console.error("bridge: saving the prompt failed", error);
    announce(key, "⚠ Not saved — use Copy prompt so the text is not lost");
    if (promptSaveSeq.get(field) === seq) saveHandoffDraft(handoffId, submitted);
  }
}

function handoffDraftKey(handoffId) {
  return "bridge.handoff-draft." + handoffId;
}

function saveHandoffDraft(handoffId, value) {
  let store;
  try {
    store = window.sessionStorage;
  } catch (error) {
    return;
  }
  if (!store) return;
  try {
    store.setItem(handoffDraftKey(handoffId), value);
  } catch (error) {
    // Blocked or full storage: the draft simply is not persisted this time.
  }
}

function clearHandoffDraft(handoffId) {
  let store;
  try {
    store = window.sessionStorage;
  } catch (error) {
    return;
  }
  if (!store) return;
  try {
    store.removeItem(handoffDraftKey(handoffId));
  } catch (error) {
    // Ignore -- nothing to clean up if storage will not answer.
  }
}

// Restores a handoff prompt that failed to save before a navigation swapped
// its field out. Unlike the compose draft (a brand-new prompt with no server
// copy), the server ALWAYS pre-fills this field with its own last-saved
// value, so the restore is unconditional on the field being empty and instead
// keyed on the draft actually differing from what just got rendered -- and it
// re-announces the warning, since the restored text is still not saved.
function restoreHandoffDrafts() {
  if (typeof document === "undefined" || !document.querySelectorAll) return;
  let store;
  try {
    store = window.sessionStorage;
  } catch (error) {
    return;
  }
  if (!store) return;

  document.querySelectorAll("[data-prompt-handoff]").forEach((field) => {
    const handoffId = field.getAttribute("data-prompt-handoff");
    let draft;
    try {
      draft = store.getItem(handoffDraftKey(handoffId));
    } catch (error) {
      return;
    }
    if (draft === null || draft === field.value) return;

    field.value = draft;
    announce(
      `[data-prompt-status="${field.id}"]`,
      "⚠ Not saved — use Copy prompt so the text is not lost",
    );
  });
}

document.addEventListener("focusout", (event) => {
  const field = event.target.closest("[data-prompt-handoff]");
  if (field) savePrompt(field);
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-launch-button]");
  if (!button) return;

  const id = button.getAttribute("data-launch-button");
  const band = document.querySelector(`[data-launch="${id}"]`);
  if (!band) return;

  const key = `[data-launch-status="${id}"]`;
  const promptId = band.getAttribute("data-launch-prompt");
  const field = promptId ? document.getElementById(promptId) : null;
  const body = window.bridgeLaunchBody(
    id, band.getAttribute("data-launch-path"),
  );
  // The field's current value is sent rather than relying on the blur save
  // having landed first: clicking ▶ fires blur and click back to back, and the
  // PATCH is in flight while the launch is being built.
  if (field) {
    body.prompt = field.value;
    body.handoff_id = band.getAttribute("data-launch-handoff");
  }

  // "I got your click" is instant and separate from "it started": a local
  // osascript spawn is fast enough that a spinner would only flash.
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  announce(key, LAUNCHING);

  try {
    const response = await fetch("/api/launch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      // Refused before any launch was recorded — nothing queued, an oversize
      // prompt, no `claude` on PATH. There is nothing to copy that the user
      // does not already have on screen, so this only reports.
      announce(key, `⚠ Not launched — ${data.detail || `HTTP ${response.status}`}`);
      return;
    }

    if (data.outcome === "failed") {
      // Glyph AND words, never a red border alone, and the prompt goes to the
      // clipboard automatically so the user can run it by hand.
      const text = data.prompt ?? (field ? field.value : "");
      const copied = await window.bridgeCopy(text, field);
      console.error("bridge: launch failed", data.error);
      announce(
        key,
        copied.startsWith("✓")
          ? "⚠ Launch failed — prompt copied, paste it in your terminal"
          : "⚠ Launch failed — press ⌘C to copy the prompt, then paste it in your terminal",
      );
      return;
    }

    announce(key, "✓ Launched — the session is opening in Terminal");
  } catch (error) {
    const text = field ? field.value : "";
    const copied = await window.bridgeCopy(text, field);
    console.error("bridge: launch request failed", error);
    announce(
      key,
      copied.startsWith("✓")
        ? "⚠ Launch failed — prompt copied, paste it in your terminal"
        : "⚠ Launch failed — the panel did not answer",
    );
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
});

// The workspace's empty-state primary button ("Start session") has no queued
// handoff to launch, so its band points `data-launch-prompt` at the ad hoc
// compose textarea instead — the click handler above already reads that field
// into the request body. The button renders `disabled` (the compose box starts
// empty), and this enables it only while there is text, so the primary action
// can never fire a promptless /api/launch that would 422. A handoff-backed band
// names its own prompt (`handoff-<id>`), never a compose id, so this leaves
// those always-launchable buttons untouched.
document.addEventListener("input", (event) => {
  const field = event.target.closest("[data-compose-prompt]");
  if (!field) return;
  const band = document.querySelector(`[data-launch-prompt="${field.id}"]`);
  if (band) {
    const button = band.querySelector("[data-launch-button]");
    if (button) button.disabled = field.value.trim() === "";
  }
  saveComposeDraft(field);
});

// The compose textarea has no server-side record (unlike the handoff prompt,
// which `savePrompt` above flushes to the server on `onLeave`), so a
// navigation swap destroys it with nothing to restore it from. `sessionStorage`
// is the right store for that draft: it survives in-tab navigations and
// reloads, and clears when the tab closes -- the correct lifetime for an
// ephemeral new-session prompt nobody asked Bridge to remember forever.
// Guarded the same way `prefillLaunchDefaults` guards `localStorage`, so a
// blocked or full `sessionStorage` is a silent no-op rather than a broken
// input handler.
function saveComposeDraft(field) {
  let store;
  try {
    store = window.sessionStorage;
  } catch (error) {
    return;
  }
  if (!store) return;

  const key = "bridge.compose." + field.id;
  try {
    if (field.value.trim() === "") store.removeItem(key);
    else store.setItem(key, field.value);
  } catch (error) {
    // Blocked or full storage: the draft simply is not persisted this time.
  }
}

// Restores a compose draft after a router swap re-renders the field empty.
// Only fires for a field that is CURRENTLY empty -- the server-rendered
// default always wins over a stale draft if the field already carries text
// (e.g. a server-side prefill), and this never overwrites something the user is
// mid-edit on. Re-applies the launch button's enable rule so a restored
// non-empty draft does not leave the primary action looking disabled.
function restoreComposeDrafts() {
  if (typeof document === "undefined" || !document.querySelectorAll) return;
  let store;
  try {
    store = window.sessionStorage;
  } catch (error) {
    return;
  }
  if (!store) return;

  document.querySelectorAll("[data-compose-prompt]").forEach((field) => {
    if (field.value !== "") return;
    let draft;
    try {
      draft = store.getItem("bridge.compose." + field.id);
    } catch (error) {
      return;
    }
    if (!draft) return;

    field.value = draft;
    const band = document.querySelector(`[data-launch-prompt="${field.id}"]`);
    if (band) {
      const button = band.querySelector("[data-launch-button]");
      if (button) button.disabled = field.value.trim() === "";
    }
  });
}

// Shared with schedule.js: a successful compose action (immediate launch or
// schedule) clears the field PROGRAMMATICALLY, which fires no `input` event --
// so without this, `saveComposeDraft`'s sessionStorage entry survives to
// resurrect the just-launched prompt on the next swap, and the launch button
// (armed by that same event) stays enabled over the now-empty field.
window.bridgeClearComposeField = function bridgeClearComposeField(field) {
  field.value = "";
  saveComposeDraft(field);
  const band = document.querySelector(`[data-launch-prompt="${field.id}"]`);
  if (band) {
    const button = band.querySelector("[data-launch-button]");
    if (button) button.disabled = true;
  }
};

// Dismiss a queued handoff from the workspace's Current tab. Reuses the same
// PATCH the handoff prompt already saves through — only the body differs —
// so no new write path is introduced. No reload: the already-rendered
// handoff section, its launch band, and its dismiss button swap `hidden` in
// place, and the empty-state paragraph is revealed only once no handoff
// section remains on the page.
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-handoff-dismiss]");
  if (!button) return;

  const id = button.getAttribute("data-handoff-dismiss");
  const key = `[data-handoff-dismiss-status="${id}"]`;
  button.disabled = true;
  try {
    const response = await fetch(`/api/handoff/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "dismissed" }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const section = document.querySelector(`[data-handoff-section="${id}"]`);
    if (section) section.hidden = true;

    // The launch band that was driving THIS handoff -- matched by
    // `data-launch-handoff`, not by the band's own id, since the band's id is
    // keyed off the handoff, not the project. The compose box (always
    // rendered) is the page's one "start a session" affordance,
    // so there is nothing left to demote this band to -- it and its dismiss
    // button just hide alongside the section. The status span stays out of
    // this: it is a SIBLING of the button, never inside it, so it is left in
    // the accessibility tree for `announce` below to reach.
    const band = document.querySelector(`[data-launch-handoff="${id}"]`);
    if (band) band.hidden = true;
    button.hidden = true;

    // The empty-state is only true once every queued handoff is gone -- a
    // sibling handoff still showing means "no session in progress" would be
    // a lie. `:not([hidden])` is left out of the selector itself (the
    // mini-DOM harness only models tag/class/id/attribute parts, never a
    // pseudo-class) and done instead with a plain array filter.
    const sections = Array.from(document.querySelectorAll("[data-handoff-section]"));
    if (sections.every((el) => el.hidden)) {
      const empty = document.querySelector(`[data-handoff-empty]`);
      if (empty) empty.hidden = false;
    }

    announce(key, "✓ Dismissed");
  } catch (error) {
    console.error("bridge: dismissing the handoff failed", error);
    announce(key, "⚠ Not dismissed — try again");
  } finally {
    button.disabled = false;
  }
});

// Prefill the launch band's model/effort selects and the schedule form's mode
// select from the browser-local "safe launch defaults" the Settings page
// stores (`bridge.launch.model` / `bridge.launch.effort` / `bridge.launch.mode`).
// This is the ONLY consumer of those keys.
//
// It NEVER reads or writes any permission value and NEVER touches
// `[data-launch-perm]`: the live permission control is armed solely by its own
// server-rendered "Ask as usual" default, per the invariant that permission is
// never persisted or pre-armed. A stored value is applied only when it matches
// an existing `<option>`; anything else is skipped, and a missing control is a
// no-op. Guarded so a sparser DOM (or a browser with no localStorage) is safe.
function prefillLaunchDefaults() {
  if (typeof document === "undefined" || !document.querySelectorAll) return;
  let store;
  try {
    store = window.localStorage;
  } catch (error) {
    return;
  }
  if (!store) return;

  const applyStored = (selector, value, respectHandoff) => {
    if (value === null) return;
    document.querySelectorAll(selector).forEach((el) => {
      // A band that carries a handoff keeps its server-selected suggestion:
      // the contextual model/effort the handoff proposed wins over the generic
      // browser-wide default. Only handoff-free bands take the stored default.
      if (respectHandoff) {
        const band = el.closest && el.closest("[data-launch]");
        if (band && band.getAttribute("data-launch-handoff") !== null) return;
      }
      if (Array.prototype.some.call(el.options || [], (opt) => opt.value === value)) {
        el.value = value;
      }
    });
  };

  applyStored("[data-launch-model]", store.getItem("bridge.launch.model"), true);
  applyStored("[data-launch-effort]", store.getItem("bridge.launch.effort"), true);
  // Mode has no per-band handoff suggestion, so it applies unconditionally.
  applyStored("[data-schedule-mode]", store.getItem("bridge.launch.mode"), false);
}

// Detaching a focused node does NOT fire focusout in any browser -- a full
// document navigation did, which is why this was safe before the shell
// persisted. Without this flush an edit made and then navigated away from is
// discarded silently, and the prompt cannot be rebuilt from transcripts.
//
// The hook itself does not await anything -- it only hands back the promise
// `bridgePage.leave()` (router.js) awaits before it swaps. That is what lets a
// failed save's warning land in `[data-prompt-status]` before the swap
// discards it, without ever blocking synchronously here.
if (window.bridgePage) {
  window.bridgePage.onLeave(() => Promise.all(
    Array.from(document.querySelectorAll("[data-prompt-handoff]")).map(savePrompt),
  ));
  window.bridgePage.onEnter(prefillLaunchDefaults);
  window.bridgePage.onEnter(restoreComposeDrafts);
  window.bridgePage.onEnter(restoreHandoffDrafts);
}
if (!window.bridgePage) {
  prefillLaunchDefaults();
  restoreComposeDrafts();
  restoreHandoffDrafts();
}
