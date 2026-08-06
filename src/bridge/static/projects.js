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

  // Plain text, not a link: the workspace route 404s for a hidden project (no
  // card -> None), so a `/project/{id}` link here would be a nav dead-end. This
  // mirrors the server-rendered hidden row in projects.html exactly; Restore is
  // the only action a hidden project offers.
  const label = document.createElement("span");
  label.className = "hidden-project__name";
  label.textContent = name;

  const status = document.createElement("span");
  status.className = "card__note";
  status.textContent = "hidden";

  const restore = document.createElement("button");
  restore.type = "button";
  restore.className = "btn";
  restore.setAttribute("data-project-restore", projectId);
  restore.setAttribute("aria-label", `Restore ${name} to the dashboard`);
  restore.textContent = "Restore";

  li.append(label, " ", status, " ", restore);
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
    // The dashboard's card names itself in an `<h2>`; the Projects index row
    // (`project_summary_row`) names itself in a `.project-row__name` span
    // instead -- both are checked so Hide announces the right name on either
    // page rather than throwing on a null `<h2>` lookup.
    const nameNode = card ? card.querySelector("h2, .project-row__name") : null;
    const name = nameNode ? nameNode.textContent.trim() : id;
    try {
      await patchProject(id, "hidden");
      // On the workspace there is no `[data-project-card]` ancestor to fold
      // into the hidden list, and a reload here would 404 now that the project
      // is hidden -- so send the user to a page that still exists rather than
      // silently doing nothing. On /projects the card is present, so the row
      // moves into the hidden list as before.
      if (!card) {
        // Go through the router when it is present so the shell survives; a hard
        // assign would tear down the SSE connection and reload every script.
        if (window.bridgeNavigate) window.bridgeNavigate("/projects");
        else window.location.assign("/projects");
        return;
      }
      const list = document.querySelector("[data-hidden-list]");
      if (list) list.append(hiddenRow(id, name));
      bumpHiddenCount(1);
      card.remove();
      // Never fail silently -- a success says so, matching pin/restore.
      say(`[data-project-status="${id}"]`, "✓ Hidden");
    } catch (error) {
      // The card stays, so its own status node is still on screen to say why.
      console.error("bridge: hiding the project failed", error);
      say(`[data-project-status="${id}"]`, "⚠ Not hidden");
    }
    return;
  }

  const filterButton = event.target.closest("[data-projects-filter]");
  if (filterButton) {
    // One filter pressed at a time: the others' `aria-pressed` is what tells
    // a screen reader (and `applyProjectsFilter`) which is active, so it is
    // the state, not a class kept separately that could disagree with it.
    document.querySelectorAll("[data-projects-filter]").forEach((btn) => {
      btn.setAttribute("aria-pressed", String(btn === filterButton));
    });
    applyProjectsFilter();
    return;
  }

  const viewButton = event.target.closest("[data-projects-view-button]");
  if (viewButton) {
    applyProjectsView(viewButton.getAttribute("data-projects-view-button"));
    // Persisted because Bridge is server-rendered: every nav click is a full
    // page load, so a layout choice held only in memory would revert the next
    // time the user came back to this page. base.html's inline head script is
    // what reads it back, pre-paint.
    try {
      localStorage.setItem("bridge.projectsView", currentProjectsView());
    } catch (error) {
      // A blocked or full localStorage must not cost the user the toggle
      // itself -- the view still changed, it just will not outlive the page.
      console.error("bridge: remembering the projects layout failed", error);
    }
    return;
  }

  // The way out of a zero-result state. Resets both inputs at once because
  // either one alone can be what emptied the list, and leaving the other set
  // would land the user on a second empty page.
  const clear = event.target.closest("[data-projects-clear]");
  if (clear) {
    const search = document.querySelector("[data-projects-search]");
    if (search) search.value = "";
    document.querySelectorAll("[data-projects-filter]").forEach((btn) => {
      btn.setAttribute("aria-pressed",
                       String(btn.getAttribute("data-projects-filter") === "all"));
    });
    applyProjectsFilter();
    if (search) search.focus();
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

// --- List / grid layout -----------------------------------------------------
//
// The layout itself is pure CSS hanging off `data-projects-view` on <html>;
// this only moves the attribute and keeps the two buttons' `aria-pressed` in
// agreement with it. `aria-pressed` is the state -- read from the DOM rather
// than mirrored in a variable that could disagree with it -- matching how the
// filter group and `.btn--pin` already work. No rows are rebuilt and nothing
// is fetched, so switching layout cannot race a pin/hide PATCH in flight.

function currentProjectsView() {
  return document.documentElement.getAttribute("data-projects-view") === "grid"
    ? "grid" : "list";
}

function applyProjectsView(view) {
  const grid = view === "grid";
  if (grid) document.documentElement.setAttribute("data-projects-view", "grid");
  else document.documentElement.removeAttribute("data-projects-view");
  document.querySelectorAll("[data-projects-view-button]").forEach((btn) => {
    btn.setAttribute("aria-pressed",
                     String(btn.getAttribute("data-projects-view-button") === view));
  });
}

// --- Search + filter (progressive enhancement) ------------------------------
//
// The server renders the full stable list, already in `cards.sort_key`
// order; this only toggles `hidden` on rows already in the DOM. No fetch, no
// reload -- a search keystroke or a filter click must never race the
// pin/hide/restore handlers above, which is exactly why this never rebuilds
// a row, only shows or hides one that already exists.

function normalizeProjectsQuery(value) {
  return (value || "").toLowerCase();
}

// "Needs attention" is not a `data-project-state` value of its own -- it is
// the same predicate `projects_view.build_projects` counts under that name
// (a queued handoff, a running session, or stale dirty work), so the three
// states it covers have to be spelled out here rather than added as a fourth
// row state that would have to be kept in sync with the model's own.
function projectsMatchesFilter(state, filter) {
  if (filter === "all") return true;
  if (filter === "needs_attention") {
    return state === "queued" || state === "running" || state === "stale";
  }
  return state === filter;
}

function applyProjectsFilter() {
  const list = document.querySelector("[data-projects-list]");
  if (!list) return; // not on the Projects page

  const pressed = document.querySelector('[data-projects-filter][aria-pressed="true"]');
  const filter = pressed ? pressed.getAttribute("data-projects-filter") : "all";
  const showingHidden = filter === "hidden";
  const search = document.querySelector("[data-projects-search]");
  const query = normalizeProjectsQuery(search ? search.value : "");

  list.hidden = showingHidden;
  const hiddenSection = document.querySelector("[data-hidden-projects]");
  if (hiddenSection) hiddenSection.hidden = !showingHidden;

  let shown = 0;
  if (showingHidden) {
    document.querySelectorAll("[data-hidden-project]").forEach((row) => {
      const visible = !query || normalizeProjectsQuery(row.textContent).includes(query);
      row.hidden = !visible;
      if (visible) shown += 1;
    });
  } else {
    document.querySelectorAll("[data-project-row-item]").forEach((row) => {
      const name = normalizeProjectsQuery(row.getAttribute("data-project-name"));
      const path = normalizeProjectsQuery(row.getAttribute("data-project-path"));
      const matchesQuery = !query || name.includes(query) || path.includes(query);
      const visible = matchesQuery
        && projectsMatchesFilter(row.getAttribute("data-project-state"), filter);
      row.hidden = !visible;
      if (visible) shown += 1;
    });
    // Group-aware: a group with no visible rows collapses out of the way, and
    // a group that still has matches is forced open whenever a search or a
    // non-"all" filter is active -- otherwise the result the user is looking
    // for could sit behind a summary that started collapsed. When neither is
    // active, the server-rendered `open` state (and any the user set by hand)
    // is left alone.
    const narrowing = Boolean(query) || filter !== "all";
    document.querySelectorAll("[data-project-group]").forEach((group) => {
      const hasVisible = Array.from(
        group.querySelectorAll("[data-project-row-item]"),
      ).some((row) => !row.hidden);
      group.hidden = !hasVisible;
      if (hasVisible && narrowing) group.open = true;
    });
  }

  const count = document.querySelector("[data-projects-count]");
  if (count) count.textContent = `${shown} project${shown === 1 ? "" : "s"} shown`;

  const empty = document.querySelector("[data-projects-empty]");
  if (empty) empty.hidden = shown !== 0;

  // A zero-result state that does not say what was searched for leaves the user
  // guessing whether they mistyped or the project is genuinely absent -- so the
  // query is echoed back verbatim, and the two ways of reaching zero get
  // different sentences because they have different ways out. `textContent`
  // (never innerHTML) is what makes echoing untrusted input safe here.
  const emptyText = document.querySelector("[data-projects-empty-text]");
  if (emptyText) {
    if (query && filter !== "all") {
      emptyText.textContent =
        `No ${showingHidden ? "hidden " : ""}projects match "${search.value}" in this filter.`;
    } else if (query) {
      emptyText.textContent = `No projects match "${search.value}".`;
    } else {
      emptyText.textContent = "No projects in this filter.";
    }
  }
  // Offered only when there is something to clear: on an unfiltered, unsearched
  // page zero rows means zero projects, and a Clear button would do nothing.
  const clear = document.querySelector("[data-projects-clear]");
  if (clear) clear.hidden = !(query || filter !== "all");
}

document.addEventListener("input", (event) => {
  if (event.target.closest("[data-projects-search]")) applyProjectsFilter();
});

// Both run on every page view, not once at load. The server renders the "all"
// filter with no query, and projects.html always renders List as the pressed
// button -- these two calls are what reconcile that with the stored preference
// and the live search box. Run once, a swapped navigation keeps the server's
// text and announces the wrong button.
if (window.bridgePage) {
  window.bridgePage.onEnter(() => {
    applyProjectsFilter();
    applyProjectsView(currentProjectsView());
  });
} else {
  applyProjectsFilter();
  applyProjectsView(currentProjectsView());
}
