// Copy-to-clipboard for queued handoff prompts.
//
// Delegated from the document, so one listener serves every card and cards can
// be re-rendered without rebinding.
//
// On the http://127.0.0.1 origin the Clipboard API is available, but it can
// still reject — denied permission, or a document that is not focused. The
// affordance must not dead-end there, so the fallback selects the text and says
// which key finishes the job.
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-target]");
  if (!button) return;

  const id = button.getAttribute("data-copy-target");
  const source = document.getElementById(id);
  const status = document.querySelector(`[data-copy-status="${id}"]`);
  if (!source) return;

  const announce = (message) => {
    if (status) status.textContent = message;
  };

  try {
    await navigator.clipboard.writeText(source.textContent);
    announce("✓ Copied to clipboard");
  } catch (error) {
    const range = document.createRange();
    range.selectNodeContents(source);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    source.focus();
    announce("⚠ Selected — press ⌘C to copy");
  }
});
