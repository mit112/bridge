// Copy-to-clipboard for queued handoff prompts.
//
// Delegated from the document, so one listener serves every card and cards can
// be re-rendered without rebinding.
//
// On the http://127.0.0.1 origin the Clipboard API is available, but it can
// still reject — denied permission, or a document that is not focused. The
// affordance must not dead-end there, so the fallback selects the text and says
// which key finishes the job.

// Shared with launch.js, which has to put the prompt on the clipboard when a
// launch fails. Extracted rather than duplicated: one clipboard behaviour, one
// fallback, one set of words.
//
// Returns the message to announce, so both callers report the same thing.
window.bridgeCopy = async function bridgeCopy(text, source) {
  try {
    await navigator.clipboard.writeText(text);
    return "✓ Copied to clipboard";
  } catch (error) {
    if (source) bridgeSelectAll(source);
    return "⚠ Selected — press ⌘C to copy";
  }
};

// The prompt is a <textarea> now, and `textContent` on a textarea is the
// server-rendered text, NOT what the user has typed. Reading `.value` is what
// keeps Copy from handing over a stale prompt.
window.bridgeText = function bridgeText(element) {
  return "value" in element ? element.value : element.textContent;
};

function bridgeSelectAll(element) {
  // A form control selects itself; ranges are for the non-form case.
  if (typeof element.select === "function") {
    element.focus();
    element.select();
    return;
  }
  const range = document.createRange();
  range.selectNodeContents(element);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  element.focus();
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-target]");
  if (!button) return;

  const id = button.getAttribute("data-copy-target");
  const source = document.getElementById(id);
  const status = document.querySelector(`[data-copy-status="${id}"]`);
  if (!source) return;

  const message = await window.bridgeCopy(window.bridgeText(source), source);
  if (status) status.textContent = message;
});
