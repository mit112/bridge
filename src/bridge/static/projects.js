// Hide a project from the dashboard, and restore one from the list at its foot.
//
// One delegated click listener, matching copy.js and launch.js.
//
// Neither action reloads the page. A reload would discard a half-typed prompt in
// another card, and the prompt is the one thing on this page Bridge cannot
// rebuild from transcripts — launch.js saves on `focusout`, so clicking Hide
// puts a PATCH in flight that a reload would race. So hiding moves the card's
// row into the hidden list, and restoring moves it back out and asks for a
// reload in words rather than performing one. Rebuilding a whole card in
// JavaScript would mean duplicating the template, which is the innerHTML pattern
// live.js exists to avoid.

// Only the fields actually being changed reach the server: JSON.stringify drops
// keys whose value is `undefined`, so an omitted argument omits the key. Guarding
// each one by hand was measurably equivalent to this — falsify proved it — and
// the server rejects a body that ends up with neither.
async function patchProject(projectId, status, pinned) {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status, pinned }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
}

function say(selector, message) {
  const node = document.querySelector(selector);
  if (node) node.textContent = message;
}

// The count in the summary is the only thing that says the list is worth
// opening, so it moves with the list rather than waiting for a reload.
function bumpHiddenCount(delta) {
  const details = document.querySelector("[data-hidden-projects]");
  const count = document.querySelector("[data-hidden-count]");
  if (!count || !details) return;
  const next = Math.max(0, Number(count.textContent || 0) + delta);
  count.textContent = String(next);
  if (next > 0) details.removeAttribute("hidden");
  else details.setAttribute("hidden", "");
}

function hiddenRow(projectId, name) {
  const li = document.createElement("li");
  li.setAttribute("data-hidden-project", projectId);

  const link = document.createElement("a");
  link.href = `/project/${encodeURIComponent(projectId)}`;
  link.textContent = name;

  const status = document.createElement("span");
  status.className = "card__note";
  status.textContent = "hidden";

  const restore = document.createElement("button");
  restore.type = "button";
  restore.className = "btn";
  restore.setAttribute("data-project-restore", projectId);
  restore.setAttribute("aria-label", `Restore ${name} to the dashboard`);
  restore.textContent = "Restore";

  li.append(link, " ", status, " ", restore);
  return li;
}

document.addEventListener("click", async (event) => {
  const pin = event.target.closest("[data-project-pin]");
  if (pin) {
    const id = pin.getAttribute("data-project-pin");
    // `aria-pressed` is the state, so it is what the toggle reads rather than a
    // second copy kept somewhere else that could disagree with it.
    const next = pin.getAttribute("aria-pressed") !== "true";
    try {
      await patchProject(id, undefined, next);
      pin.setAttribute("aria-pressed", String(next));
      // Not reordered here. A pinned card belongs at the top, but placing it
      // there client-side would use a different tiebreak from the server's and
      // the order would visibly reshuffle on the next load.
      say(`[data-project-status="${id}"]`,
          next ? "✓ Pinned — reload to re-sort" : "✓ Unpinned — reload to re-sort");
    } catch (error) {
      console.error("bridge: pinning the project failed", error);
      say(`[data-project-status="${id}"]`, next ? "⚠ Not pinned" : "⚠ Not unpinned");
    }
    return;
  }

  const hide = event.target.closest("[data-project-hide]");
  if (hide) {
    const id = hide.getAttribute("data-project-hide");
    const card = hide.closest("[data-project-card]");
    const name = card ? card.querySelector("h2").textContent.trim() : id;
    try {
      await patchProject(id, "hidden");
      const list = document.querySelector("[data-hidden-list]");
      if (list) list.append(hiddenRow(id, name));
      bumpHiddenCount(1);
      if (card) card.remove();
    } catch (error) {
      // The card stays, so its own status node is still on screen to say why.
      console.error("bridge: hiding the project failed", error);
      say(`[data-project-status="${id}"]`, "⚠ Not hidden");
    }
    return;
  }

  const restore = event.target.closest("[data-project-restore]");
  if (!restore) return;

  const id = restore.getAttribute("data-project-restore");
  try {
    await patchProject(id, "active");
    const row = document.querySelector(`[data-hidden-project="${id}"]`);
    if (row) row.remove();
    bumpHiddenCount(-1);
    say("[data-hidden-status]", "✓ Restored — reload to see its card");
  } catch (error) {
    console.error("bridge: restoring the project failed", error);
    say("[data-hidden-status]", "⚠ Not restored");
  }
});
