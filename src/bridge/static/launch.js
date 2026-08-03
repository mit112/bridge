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

// Save an edited prompt when focus leaves the field, and only when the text
// actually changed — `focusout` (which bubbles, unlike `blur`) fires on every
// tab-through, and a PATCH per tab-through would re-journal an unchanged prompt.
document.addEventListener("focusout", async (event) => {
  const field = event.target.closest("[data-prompt-handoff]");
  if (!field) return;

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

// Dismiss a queued handoff from the workspace's Current tab. Reuses the same
// PATCH the handoff prompt already saves through — only the body differs —
// so no new write path is introduced. No reload: the already-rendered
// handoff section and empty-state paragraph swap `hidden` in place, and the
// launch band that was driving the queued handoff demotes to a plain ad hoc
// launch rather than keep pointing at a prompt that no longer exists.
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
    // keyed off the project, not the handoff. Demoted to a plain launch: the
    // attributes that named the now-dismissed prompt are removed, and the
    // primary button's label falls back to the empty-state wording.
    const band = document.querySelector(`[data-launch-handoff="${id}"]`);
    if (band) {
      band.removeAttribute("data-launch-handoff");
      band.removeAttribute("data-launch-prompt");
      const launchButton = band.querySelector("[data-launch-button]");
      if (launchButton && launchButton.textContent.trim() === "Continue in Terminal") {
        launchButton.textContent = "Start session";
      }
    }

    const empty = document.querySelector(`[data-handoff-empty]`);
    if (empty) empty.hidden = false;

    announce(key, "✓ Dismissed");
  } catch (error) {
    console.error("bridge: dismissing the handoff failed", error);
    announce(key, "⚠ Not dismissed — try again");
  } finally {
    button.disabled = false;
  }
});
