// Swap the content region instead of replacing the document.
//
// Bridge is server-rendered, so every nav click used to tear down and rebuild
// the entire page -- including the SSE connection, the scroll position, and all
// module state. Replacing one dense page with another in a single frame is the
// abrupt whole-screen change this exists to remove; no amount of CSS could fix
// it, because the teardown IS the effect.
//
// Progressive enhancement is not decoration here. Every link stays an ordinary
// link: this file only ever calls preventDefault on a navigation it is certain
// it can complete, and ANY failure falls back to a real one.

const SWAPPABLE = new Set(["/", "/projects", "/schedule", "/diagnostics", "/settings"]);

function swappable(url) {
  return url.origin === window.location.origin && SWAPPABLE.has(url.pathname);
}

function parseFragment(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const body = doc.querySelector(".shell__body");
  const status = doc.querySelector(".shell-status");
  const active = doc.querySelector('meta[name="bridge-active"]');
  if (!body || !status) return null;
  return {
    body,
    status,
    title: doc.querySelector("title") ? doc.querySelector("title").textContent : null,
    active: active ? active.getAttribute("content") : null,
  };
}

function setActiveNav(active) {
  // The nav lives in the persistent sidebar, so nothing else would ever correct
  // it -- aria-current would stay on whichever page the session started with.
  document.querySelectorAll(".sidebar__link").forEach((link) => {
    link.removeAttribute("aria-current");
  });
  if (!active) return;
  const current = document.querySelector(`.sidebar__link[href="${activeHref(active)}"]`);
  if (current) current.setAttribute("aria-current", "page");
}

function activeHref(active) {
  return active === "overview" ? "/" : `/${active}`;
}

function applyFragment(parsed) {
  const body = document.querySelector(".shell__body");
  const status = document.querySelector(".shell-status");
  if (!body || !status) return false;
  body.replaceWith(parsed.body);
  status.replaceWith(parsed.status);
  if (parsed.title) document.title = parsed.title;
  setActiveNav(parsed.active);
  return true;
}

// A swap moves neither focus nor the screen reader's attention -- the browser
// does that for a real navigation and does nothing for a DOM replacement. Both
// are required, not polish.
function announceArrival() {
  const main = document.getElementById("main");
  if (main && main.focus) main.focus();
  window.scrollTo(0, 0);
}

async function navigate(href, { push = true } = {}) {
  const url = new URL(href, window.location.href);
  if (!swappable(url)) { window.location.assign(href); return; }
  try {
    // Awaited -- not fire-and-forget. A leave hook's own async work (launch.js's
    // prompt flush) must settle before the fragment fetch and the swap it feeds,
    // or a failed save's warning lands on a status node that is already gone.
    // bridgePage.leave() (shell.js) returns a promise for exactly this.
    await window.bridgePage.leave();
    const response = await fetch(url.href, {
      headers: { "X-Bridge-Fragment": "1" },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const parsed = parseFragment(await response.text());
    if (!parsed || !applyFragment(parsed)) throw new Error("unusable fragment");
    if (push) window.history.pushState({ bridge: true }, "", url.href);
    window.bridgePage.enter();
    announceArrival();
  } catch (error) {
    // Never strand the user on a link that did nothing. A real navigation is
    // always correct -- it is only ever slower.
    console.error("bridge: swap failed, falling back to a full load", error);
    window.location.assign(href);
  }
}

window.bridgeNavigate = navigate;

if (document.addEventListener) {
  document.addEventListener("click", (event) => {
    // The standard opt-outs: a modified or non-primary click must keep its
    // browser meaning, or the router breaks cmd-click-to-new-tab.
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest && event.target.closest("a[href]");
    if (!link) return;
    if (link.hasAttribute("download") || link.hasAttribute("target")) return;
    const url = new URL(link.getAttribute("href"), window.location.href);
    if (!swappable(url)) return;
    event.preventDefault();
    navigate(url.href);
  });

  window.addEventListener("popstate", () => {
    navigate(window.location.href, { push: false });
  });
}
