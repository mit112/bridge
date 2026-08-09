// The update banner: shown only when `GET /api/diagnostics`'s `update.state`
// is "behind". Lives in the persistent shell (outside `#main`), so router.js
// never detaches it -- the module-scope lookups below are safe for the same
// reason shell.js's own `document.documentElement` capture is.
//
// Dismissal is keyed by the OFFERED SHA (`bridge:update-dismissed:<sha>`), not
// by a single flag: dismissing today's offer must never suppress a later,
// different commit. Nothing here clears that key on success -- a fresh
// `installed_sha` after a real update means the checker stops offering that
// SHA at all, which already hides the banner without needing to.
//
// A failed attempt never dismisses anything: the banner (and its error) stay
// on screen so "Update now" is still there to retry, and the copy-able
// `bridge update` fallback line is on the page regardless of whether the
// fetch above ever succeeds -- it is a separate, JS-independent path to the
// exact same action.
(function () {
  "use strict";
  var banner = document.getElementById("update-banner");
  if (!banner) return;

  var applyButton = document.getElementById("update-banner__apply");
  var dismissButton = document.getElementById("update-banner__dismiss");
  var fromEl = document.getElementById("update-banner__from");
  var toEl = document.getElementById("update-banner__to");
  var statusEl = banner.querySelector("[data-update-status]");
  var copyButton = document.getElementById("update-banner__copy");
  var copyStatusEl = document.getElementById("update-banner__copy-status");
  var cmdEl = document.getElementById("update-banner__cmd");
  var tokenMeta = document.querySelector('meta[name="bridge-update-token"]');
  var token = tokenMeta ? tokenMeta.content : "";

  function dismissedKey(sha) {
    return "bridge:update-dismissed:" + sha;
  }

  function isDismissed(sha) {
    try {
      return localStorage.getItem(dismissedKey(sha)) === "1";
    } catch (error) {
      // Blocked or unavailable storage: never treat that as "dismissed".
      return false;
    }
  }

  function announce(message) {
    if (statusEl) statusEl.textContent = message;
  }

  function render(upd) {
    if (!upd || upd.state !== "behind" || !upd.latest_sha || isDismissed(upd.latest_sha)) {
      banner.hidden = true;
      return;
    }
    banner.dataset.sha = upd.latest_sha;
    banner.dataset.from = upd.installed_sha || "dev";
    if (fromEl) fromEl.textContent = (upd.installed_sha || "dev").slice(0, 12);
    if (toEl) toEl.textContent = upd.latest_sha.slice(0, 12);
    banner.hidden = false;
  }

  if (dismissButton) {
    dismissButton.addEventListener("click", function () {
      var sha = banner.dataset.sha;
      if (sha) {
        try {
          localStorage.setItem(dismissedKey(sha), "1");
        } catch (error) {
          // Blocked or full storage: the dismiss still hides the banner for
          // this page view, it just will not outlive it.
        }
      }
      banner.hidden = true;
    });
  }

  // The "always-available fallback": copying the `bridge update` command
  // works whether or not the button above ever fires successfully. Reuses
  // copy.js's `window.bridgeCopy` (clipboard write, with the select-and-tell
  // fallback launch.js also relies on) instead of a second implementation --
  // it just skips copy.js's `[data-copy-target]` delegated listener, which
  // several tests assert is entirely absent from a page with no queued
  // handoff.
  if (copyButton && cmdEl && window.bridgeCopy) {
    copyButton.addEventListener("click", function () {
      window.bridgeCopy(cmdEl.textContent, cmdEl).then(function (message) {
        if (copyStatusEl) copyStatusEl.textContent = message;
      });
    });
  }

  if (applyButton) {
    applyButton.addEventListener("click", function () {
      var sha = banner.dataset.sha;
      var from = banner.dataset.from || "dev";
      if (!sha) return;
      var ok = window.confirm(
        "Update Bridge from " + from.slice(0, 12) + " to " + sha.slice(0, 12) +
        "? The panel will restart."
      );
      if (!ok) return;

      applyButton.disabled = true;
      applyButton.setAttribute("aria-busy", "true");
      announce("Updating…");

      // A managed-LaunchAgent install is ASYNCHRONOUS: the server answers 202
      // `{accepted: true}` and a detached one-shot job installs the new build
      // AND restarts the panel out from under this page, so the SSE stream
      // drops and the page reconnects. There is no synchronous result to show
      // — the reconnect's /api/diagnostics update-state read resolves the
      // outcome — so we leave the banner in an "updating" state and keep the
      // button disabled, rather than reporting a success/failure the server
      // never sent. The unmanaged (`bridge serve`) path still returns a
      // synchronous UpdateResult and is handled exactly as before.
      var stayDisabled = false;

      fetch("/api/update", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + token,
        },
        body: JSON.stringify({ target_sha: sha }),
      })
        .then(function (response) {
          return response.json().catch(function () { return {}; }).then(function (data) {
            return { httpOk: response.ok, status: response.status, data: data };
          });
        })
        .then(function (result) {
          if (result.status === 202 || (result.data && result.data.accepted === true)) {
            stayDisabled = true;
            announce("Updating… the panel is restarting; this page will reconnect.");
            return;
          }
          if (result.httpOk && result.data && result.data.ok) {
            announce("✓ Updating — the panel will restart shortly");
            return;
          }
          var message = (result.data && result.data.error) || "unknown error";
          console.error("bridge: update failed", message);
          announce("⚠ Update failed — " + message + ". Run `bridge update` to retry.");
        })
        .catch(function (error) {
          console.error("bridge: update request failed", error);
          announce("⚠ Update request failed. Run `bridge update` to retry.");
        })
        .finally(function () {
          if (stayDisabled) return;
          applyButton.disabled = false;
          applyButton.removeAttribute("aria-busy");
        });
    });
  }

  function poll() {
    fetch("/api/diagnostics")
      .then(function (response) { return response.json(); })
      .then(function (data) { render(data.update); })
      .catch(function () {});
  }

  poll();
  setInterval(poll, 60000);
})();
