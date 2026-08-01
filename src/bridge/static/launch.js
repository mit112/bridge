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
  const model = document.querySelector(`[data-launch-model="${id}"]`);
  const effort = document.querySelector(`[data-launch-effort="${id}"]`);

  const body = {
    project_path: band.getAttribute("data-launch-path"),
    mode: "terminal",
    model: model ? model.value : null,
    effort: effort ? effort.value : null,
  };
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
