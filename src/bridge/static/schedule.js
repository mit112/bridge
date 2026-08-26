// The compose box (an ad hoc prompt per project), the "Schedule…" affordance
// on a queued handoff, and the global Scheduled section: create, edit,
// cancel, run early, and retry a `scheduled_runs` row.
//
// One delegated `click` listener, matching launch.js and copy.js, so the
// listener count stays flat no matter how many cards or scheduled rows render.

function announce(selector, message) {
  const status = document.querySelector(selector);
  if (status) status.textContent = message;
}

async function announceComposeLaunchFailure(key, field, detail) {
  const copied = await window.bridgeCopy(field.value, field);
  console.error("bridge: compose launch failed", detail);
  announce(
    key,
    copied.startsWith("✓")
      ? `⚠ Launch failed — prompt copied; ${detail}`
      : `⚠ Launch failed — press ⌘C to copy the prompt; ${detail}`,
  );
}

async function postJSON(url, method, body) {
  const response = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  return { ok: response.ok, status: response.status, data };
}

// `datetime-local` has no timezone of its own -- it is local wall-clock time,
// read and written as `YYYY-MM-DDTHH:mm`. `Date` parses that string as local
// time when it carries no offset, so round-tripping through it is what turns
// the field's value into the epoch seconds the server stores everything as.
// Exposed on `window`, matching `copy.js`'s `bridgeCopy`/`bridgeText`: this is
// the pure, DOM-free part a node harness can drive directly, and `Math.floor`
// (not `Math.round`) is load-bearing -- rounding up a sub-second remainder
// would schedule a job a second later than the wall-clock the user picked.
window.localInputToEpoch = function localInputToEpoch(value) {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
};

// The inverse, for pre-filling an <input type="datetime-local">: local
// calendar fields, zero-padded, with no trailing seconds -- the format the
// input itself requires for its `value`.
window.epochToDatetimeLocalValue = function epochToDatetimeLocalValue(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

function epochToLocalDisplay(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleString(undefined, {
    dateStyle: "medium", timeStyle: "short",
  });
}

// Paints every `[data-scheduled-for]` cell once: the server only ever stores
// epoch seconds, so the browser -- the only thing that knows the viewer's own
// timezone -- is what turns them back into a clock the viewer reads.
function paintScheduledTimes(root) {
  (root || document).querySelectorAll("[data-scheduled-for]").forEach((el) => {
    const epoch = Number(el.getAttribute("data-scheduled-for"));
    if (Number.isFinite(epoch)) el.textContent = epochToLocalDisplay(epoch);
  });
}

function paintEditInput(panel) {
  const input = panel.querySelector("[data-scheduled-edit-when]");
  if (!input) return;
  const epoch = Number(input.getAttribute("data-scheduled-epoch"));
  if (Number.isFinite(epoch)) input.value = window.epochToDatetimeLocalValue(epoch);
}

// The topbar count and the section's own `<summary>` count are both rendered
// once, on load -- nothing after that keeps them honest. Every mutation that
// changes how many jobs are `pending`/`launching` (create, cancel, run-now)
// calls this so the two numbers on the page never disagree with what just
// happened.
function bumpScheduledCount(delta) {
  const summaryCount = document.querySelector("[data-scheduled-count]");
  const topbarCount = document.querySelector("[data-topbar-scheduled]");
  [summaryCount, topbarCount].forEach((el) => {
    if (!el) return;
    el.textContent = String(Math.max(0, (Number(el.textContent) || 0) + delta));
  });
  // Rendered `hidden` when nothing was due at load -- a count update inside a
  // `hidden` <details> is invisible, so crossing zero is what reveals it.
  if (delta > 0) {
    const details = document.querySelector("[data-scheduled]");
    if (details && details.hidden) {
      details.hidden = false;
      details.open = true;
    }
  }
  // The mirror image: cancelling the last job left an open <details> reading
  // "Scheduled 0" with nothing under it. Emptiness is the LIST's, not the
  // count's -- a finished run still on screen is history worth keeping open,
  // and the count only ever counts the active ones.
  //
  // Collapsed, never `hidden`. The cancel handler has just moved focus to the
  // <summary> and the section's live region is about to announce "Cancelled";
  // hiding the <details> would take both out of the accessibility tree in the
  // same task, dropping focus to <body> and swallowing the announcement (WCAG
  // 2.4.3, 4.1.3) -- exactly what moving them off the row was meant to
  // prevent. A reload renders the empty section `hidden` anyway.
  if (delta < 0) {
    const list = document.querySelector("[data-scheduled-list]");
    const details = document.querySelector("[data-scheduled]");
    if (list && details && list.children.length === 0) details.open = false;
  }
}

// The visible state of a row, rewritten to match what the server would render
// for the status it has now: the class, the status word, and the error beside
// it. Split out from `settleRow` because a chained retry needs exactly this
// much and no more -- it must NOT grow a second Retry button.
function restateRow(row, status, error) {
  row.className = `scheduled__job scheduled__job--${status}`;
  const state = row.querySelector("[data-scheduled-state]");
  if (state) state.textContent = status;
  // Server-rendered rows keep the error in a note beside the status; a row
  // settled in place had it only in the polite live region, which is gone the
  // moment anything else is announced. A row marked "failed" with the reason
  // nowhere on screen is the state this avoids.
  let note = row.querySelector("[data-scheduled-error]");
  if (error && !note) {
    note = document.createElement("span");
    note.className = "card__note";
    note.setAttribute("data-scheduled-error", "");
    row.insertBefore(note, row.querySelector("[data-scheduled-status]"));
  }
  if (note) note.textContent = error || "";
}

// After a run-now the row still carries the controls the server rendered for a
// `pending` job -- Run now, Edit, Cancel -- and every one of them can now only
// 409. This is what makes the row agree with the database without a reload:
// the same shape, and the same order, the server would render for the status
// it just reached.
function settleRow(id, status, error) {
  const row = document.querySelector(`[data-scheduled-job="${id}"]`);
  if (!row) return;
  restateRow(row, status, error);
  row.querySelectorAll(
    "[data-scheduled-run-now], [data-scheduled-edit-toggle], " +
    "[data-scheduled-cancel], [data-scheduled-edit-panel]",
  ).forEach((el) => el.remove());
  // `indeterminate` as much as `failed`: neither is ever retried
  // automatically, so this button is the only recovery either one has.
  if (status === "failed" || status === "indeterminate" || status === "missed") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn";
    button.setAttribute("data-scheduled-retry", id);
    // Named like the server's, and for the same reason: twenty rows of a bare
    // "Retry" are twenty identical entries in a screen reader's button list.
    const label = row.getAttribute("data-scheduled-retry-label");
    button.setAttribute("aria-label", label || "Retry");
    button.textContent = "Retry";
    // Before the error note and the live region, matching the server's order
    // -- otherwise the same row tabs differently depending on whether you
    // reloaded or settled it in place.
    row.insertBefore(button, row.querySelector("[data-scheduled-error]")
      || row.querySelector("[data-scheduled-status]"));
    // The button that had focus was just removed. Focus goes to the control
    // that replaced it, in the same row (WCAG 2.4.3).
    button.focus();
    return;
  }
  // Nothing replaced the removed button, so focus falls back to the section's
  // own <summary> -- the same target the cancel path uses, and the one node
  // here that outlives every row. The dashboard's `<details>` always has one;
  // `/schedule` has no disclosure at all, so its own section-status paragraph
  // (rendered `tabindex="-1"` there for exactly this) is the fallback when no
  // <summary> exists.
  const summary = document.querySelector("[data-scheduled] summary")
    || document.querySelector("[data-scheduled-section-status]");
  if (summary) summary.focus();
}

// Repaint on every page view, not once per document. Bridge swaps only the
// content region, so binding this to a page-load event would fire exactly
// once for the whole session -- every scheduled cell rendered by a later
// swap would keep the server's raw UTC string. shell.js always loads first,
// matching settings.js's own guard for the same registry.
if (window.bridgePage) {
  window.bridgePage.onEnter(() => paintScheduledTimes());
  // A live morph re-renders /schedule in place and morph.js's leaf-text sync
  // reverts every painted cell to the server's raw UTC -- repaint after it.
  window.bridgePage.onMorph(() => paintScheduledTimes());
}

document.addEventListener("click", async (event) => {
  // --- Reveal the datetime + mode form beside a compose box or a handoff ---
  const scheduleToggle = event.target.closest("[data-schedule-toggle]");
  if (scheduleToggle) {
    const id = scheduleToggle.getAttribute("data-schedule-toggle");
    const panel = document.getElementById(id);
    if (!panel) return;
    const expanded = scheduleToggle.getAttribute("aria-expanded") === "true";
    scheduleToggle.setAttribute("aria-expanded", String(!expanded));
    panel.hidden = expanded;
    return;
  }

  // --- Compose box: run the freshly typed prompt right now ---
  const composeRun = event.target.closest("[data-compose-run]");
  if (composeRun) {
    const id = composeRun.getAttribute("data-compose-run");
    const field = document.getElementById(id);
    const key = `[data-compose-status="${id}"]`;
    if (!field || !field.value.trim()) {
      announce(key, "⚠ Nothing to run — type a prompt first");
      return;
    }
    composeRun.disabled = true;
    composeRun.setAttribute("aria-busy", "true");
    announce(key, "Launching…");
    try {
      const body = window.bridgeLaunchBody(
        composeRun.getAttribute("data-compose-launch"),
        composeRun.getAttribute("data-compose-path"),
      );
      body.prompt = field.value;
      const { ok, status, data } = await postJSON("/api/launch", "POST", body);
      if (!ok) {
        await announceComposeLaunchFailure(
          key, field, data.detail || `HTTP ${status}`,
        );
        return;
      }
      if (data.outcome === "failed") {
        await announceComposeLaunchFailure(
          key, field, data.error || "see the terminal",
        );
        return;
      }
      announce(key, "✓ Launched — the session is opening in Terminal");
      field.value = "";
    } catch (error) {
      await announceComposeLaunchFailure(
        key, field, "the panel did not answer",
      );
    } finally {
      composeRun.disabled = false;
      composeRun.removeAttribute("aria-busy");
    }
    return;
  }

  // --- Submit a schedule form: compose box or handoff, told apart by
  //     whether the panel carries `data-schedule-handoff` ---
  const scheduleSubmit = event.target.closest("[data-schedule-submit]");
  if (scheduleSubmit) {
    const panelId = scheduleSubmit.getAttribute("data-schedule-submit");
    const panel = document.getElementById(panelId);
    const key = `[data-schedule-status="${panelId}"]`;
    if (!panel) return;
    const promptId = panel.getAttribute("data-schedule-prompt");
    const field = promptId ? document.getElementById(promptId) : null;
    if (!field || !field.value.trim()) {
      announce(key, "⚠ Nothing to schedule — type a prompt first");
      return;
    }
    const when = panel.querySelector("[data-schedule-when]");
    const scheduledFor = when ? window.localInputToEpoch(when.value) : null;
    if (!scheduledFor) {
      announce(key, "⚠ Pick a date and time first");
      return;
    }
    const mode = panel.querySelector("[data-schedule-mode]");
    const handoffId = panel.getAttribute("data-schedule-handoff");
    scheduleSubmit.disabled = true;
    announce(key, "Scheduling…");
    try {
      const { ok, status, data } = await postJSON("/api/schedule", "POST", {
        project_path: panel.getAttribute("data-schedule-path"),
        prompt: field.value,
        scheduled_for: scheduledFor,
        mode: mode ? mode.value : "terminal",
        source_handoff_id: handoffId || null,
      });
      if (!ok) {
        announce(key, `⚠ Not scheduled — ${data.detail || `HTTP ${status}`}`);
        return;
      }
      announce(key, "✓ Scheduled");
      // The compose box's own prompt is cleared on success; a handoff's is
      // left alone, since it stays queued for a manual launch too.
      if (!handoffId) field.value = "";
      const toggle = document.querySelector(`[data-schedule-toggle="${panelId}"]`);
      // Focus moves to the toggle BEFORE the panel is hidden -- the button
      // just clicked is a descendant of `panel`, and hiding an ancestor of
      // the focused element silently drops focus to <body> with nothing
      // announced (WCAG 2.4.3). `key` above is a sibling of `panel`, not a
      // descendant, so the "✓ Scheduled" announcement itself is unaffected.
      if (toggle) {
        toggle.focus();
        toggle.setAttribute("aria-expanded", "false");
      }
      panel.hidden = true;
      bumpScheduledCount(1);
    } catch (error) {
      console.error("bridge: schedule request failed", error);
      announce(key, "⚠ Scheduling failed — the panel did not answer");
    } finally {
      scheduleSubmit.disabled = false;
    }
    return;
  }

  // --- The global Scheduled section: edit, cancel, run now, retry ---

  const editToggle = event.target.closest("[data-scheduled-edit-toggle]");
  if (editToggle) {
    const id = editToggle.getAttribute("data-scheduled-edit-toggle");
    const panel = document.querySelector(`[data-scheduled-edit-panel="${id}"]`);
    if (!panel) return;
    const expanded = editToggle.getAttribute("aria-expanded") === "true";
    editToggle.setAttribute("aria-expanded", String(!expanded));
    panel.hidden = expanded;
    if (!expanded) paintEditInput(panel);
    return;
  }

  const editSave = event.target.closest("[data-scheduled-edit-save]");
  if (editSave) {
    const id = editSave.getAttribute("data-scheduled-edit-save");
    const when = document.querySelector(`[data-scheduled-edit-when="${id}"]`);
    const key = `[data-scheduled-status="${id}"]`;
    const epoch = when ? window.localInputToEpoch(when.value) : null;
    if (!epoch) {
      announce(key, "⚠ Pick a date and time first");
      return;
    }
    editSave.disabled = true;
    try {
      const { ok, status, data } = await postJSON(
        `/api/schedule/${encodeURIComponent(id)}`, "PATCH", { scheduled_for: epoch },
      );
      if (!ok) {
        announce(key, `⚠ Not saved — ${data.detail || `HTTP ${status}`}`);
        return;
      }
      const row = document.querySelector(`[data-scheduled-job="${id}"]`);
      const timeEl = row ? row.querySelector("[data-scheduled-for]") : null;
      if (timeEl) {
        timeEl.setAttribute("data-scheduled-for", String(epoch));
        paintScheduledTimes(row);
      }
      when.setAttribute("data-scheduled-epoch", String(epoch));
      announce(key, "✓ Saved");
    } catch (error) {
      console.error("bridge: schedule edit failed", error);
      announce(key, "⚠ Not saved — the panel did not answer");
    } finally {
      editSave.disabled = false;
    }
    return;
  }

  const cancelButton = event.target.closest("[data-scheduled-cancel]");
  if (cancelButton) {
    const id = cancelButton.getAttribute("data-scheduled-cancel");
    const key = `[data-scheduled-status="${id}"]`;
    cancelButton.disabled = true;
    try {
      const { ok, status, data } = await postJSON(
        `/api/schedule/${encodeURIComponent(id)}`, "DELETE",
      );
      if (!ok) {
        announce(key, `⚠ Not cancelled — ${data.detail || `HTTP ${status}`}`);
        return;
      }
      const row = document.querySelector(`[data-scheduled-job="${id}"]`);
      // Both the focus target and the announcement move to the section
      // itself, which outlives every row in it -- `row` (and the cancel
      // button's own status span inside it) is about to be removed, so
      // neither can hold either one (WCAG 2.4.3, 4.1.3). Same dashboard
      // <summary> vs. `/schedule` section-status fallback as `settleRow`.
      const summary = document.querySelector("[data-scheduled] summary")
        || document.querySelector("[data-scheduled-section-status]");
      if (summary) summary.focus();
      announce("[data-scheduled-section-status]", "✓ Cancelled");
      if (row) row.remove();
      bumpScheduledCount(-1);
    } catch (error) {
      console.error("bridge: schedule cancel failed", error);
      announce(key, "⚠ Not cancelled — the panel did not answer");
    } finally {
      cancelButton.disabled = false;
    }
    return;
  }

  const runNowButton = event.target.closest("[data-scheduled-run-now]");
  if (runNowButton) {
    const id = runNowButton.getAttribute("data-scheduled-run-now");
    const key = `[data-scheduled-status="${id}"]`;
    runNowButton.disabled = true;
    announce(key, "Running…");
    try {
      const { ok, status, data } = await postJSON(
        `/api/schedule/${encodeURIComponent(id)}/run-now`, "POST",
      );
      if (!ok) {
        announce(key, `⚠ Not run — ${data.detail || `HTTP ${status}`}`);
        return;
      }
      // Claimed, then fired or failed trying -- either way the row leaves
      // `pending`/`launching`, so the active count drops regardless of
      // which the response reports.
      bumpScheduledCount(-1);
      settleRow(id, data.status, data.error);
      announce(
        key,
        data.status === "fired"
          ? "✓ Launched — the session is opening in Terminal"
          : `⚠ ${data.status}${data.error ? " — " + data.error : ""}`,
      );
    } catch (error) {
      console.error("bridge: schedule run-now failed", error);
      announce(key, "⚠ Not run — the panel did not answer");
    } finally {
      runNowButton.disabled = false;
    }
    return;
  }

  // --- Retry a failed or indeterminate schedule ---
  //
  // Through `/api/schedule/{id}/retry`, NOT `/api/launch`. The old retry
  // scraped the prompt out of the page and POSTed a plain launch with no
  // handoff at all -- so retrying a schedule created from a handoff launched
  // fine and left that handoff queued forever. The server re-reads the row, so
  // `source_handoff_id` (and the model, effort and permission the schedule was
  // created with) come along without the page having to carry any of them.
  const retryButton = event.target.closest("[data-scheduled-retry]");
  if (retryButton) {
    const id = retryButton.getAttribute("data-scheduled-retry");
    // Keyed off the ROW, not `id`: a chained retry below repoints the button
    // at the new run, and the announcement must keep landing in the row the
    // button actually lives in (WCAG 4.1.3).
    const row = retryButton.closest("[data-scheduled-job]");
    const rowId = row ? row.getAttribute("data-scheduled-job") : id;
    const key = `[data-scheduled-status="${rowId}"]`;
    retryButton.disabled = true;
    announce(key, "Retrying…");
    try {
      const { ok, status, data } = await postJSON(
        `/api/schedule/${encodeURIComponent(id)}/retry`, "POST",
      );
      if (!ok) {
        announce(key, `⚠ Retry failed — ${data.detail || `HTTP ${status}`}`);
        // 409 is "not retryable, or retried already" -- a permanent no, so the
        // control goes rather than sitting there promising a second attempt.
        if (status === 409) retryButton.remove();
        return;
      }
      if (data.status === "fired") {
        announce(key, "✓ Retried — the session is opening in Terminal");
        retryButton.remove();
        return;
      }
      announce(key, `⚠ Retry ${data.status}${data.error ? " — " + data.error : ""}`);
      // The retry is its own row, and the server allows exactly one retry per
      // row. Repointing the button at the new run is what keeps the recovery
      // loop working before a reload renders that row for itself -- and the
      // row is restated to match, so its visible status never describes one
      // run while its only control acts on another.
      retryButton.setAttribute("data-scheduled-retry", data.id);
      if (row) restateRow(row, data.status, data.error);
    } catch (error) {
      console.error("bridge: schedule retry failed", error);
      announce(key, "⚠ Retry failed — the panel did not answer");
    } finally {
      retryButton.disabled = false;
    }
    return;
  }
});
