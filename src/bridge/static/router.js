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

// The project workspace and everything inside it -- its tabs, and the history
// tables' sort/filter/pager, which only vary the query string -- share the
// `/project/{id}` path. It has a fragment mode too, so it swaps like the
// sidebar destinations instead of tearing the shell down on every tab click.
// Scoped to a numeric id so it can never widen to some other /project/... path.
const WORKSPACE_PATH = /^\/project\/\d+$/;

function swappable(url) {
  if (url.origin !== window.location.origin) return false;
  return SWAPPABLE.has(url.pathname) || WORKSPACE_PATH.test(url.pathname);
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
  // Focus the new content for the screen reader, but NOT with the browser's
  // scroll-into-view. At >=1024px the shell is a fixed 100vh cage and
  // `.shell__body` -- not the window -- is the scroll container, so focusing a
  // `#main` taller than the viewport would scroll that container to pin #main's
  // top and push the whole page header (breadcrumb, title, actions) out of
  // view. `preventScroll` keeps focus a pure a11y move.
  if (main && main.focus) main.focus({ preventScroll: true });
  // Land at the top like a real navigation. `window.scrollTo` handles the
  // document scroll below 1024px; resetting `.shell__body` handles the scroll
  // container at and above it, where `window.scrollTo` is a no-op.
  window.scrollTo(0, 0);
  const body = document.querySelector(".shell__body");
  if (body) body.scrollTop = 0;
}

// A monotonic counter, bumped once per `navigate()` call. Two rapid
// navigations (a fast double-click, or a click racing a popstate) can have
// their `leave()`/`fetch()` steps resolve in EITHER order -- nothing about
// promises guarantees the one issued first finishes first. Each call
// captures the epoch it was issued under and re-checks it after every await;
// a navigation whose epoch no longer matches the current one has been
// superseded by a newer navigation and must touch neither the DOM nor
// history, and must not fall back to a full reload of its own (now stale)
// href either -- the newer navigation is already doing the right thing.
let navEpoch = 0;

async function navigate(href, { push = true } = {}) {
  const url = new URL(href, window.location.href);
  if (!swappable(url)) { window.location.assign(href); return; }
  const epoch = ++navEpoch;
  try {
    // Awaited -- not fire-and-forget. A leave hook's own async work (launch.js's
    // prompt flush) must settle before the fragment fetch and the swap it feeds,
    // or a failed save's warning lands on a status node that is already gone.
    // bridgePage.leave() (shell.js) returns a promise for exactly this.
    await window.bridgePage.leave();
    if (epoch !== navEpoch) return;  // superseded while awaiting leave()
    const response = await fetch(url.href, {
      headers: { "X-Bridge-Fragment": "1" },
      credentials: "same-origin",
    });
    if (epoch !== navEpoch) return;  // superseded while the fetch was in flight
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const parsed = parseFragment(await response.text());
    if (!parsed || !applyFragment(parsed)) throw new Error("unusable fragment");
    if (push) window.history.pushState({ bridge: true }, "", url.href);
    window.bridgePage.enter();
    announceArrival();
  } catch (error) {
    if (epoch !== navEpoch) return;  // a newer navigation already took over
    // Never strand the user on a link that did nothing. A real navigation is
    // always correct -- it is only ever slower.
    console.error("bridge: swap failed, falling back to a full load", error);
    window.location.assign(href);
  }
}

window.bridgeNavigate = navigate;

// Shared so liverefresh.js parses fragments through the one implementation
// instead of shipping a second DOMParser path that could drift from this one.
window.bridgeFragment = { parse: parseFragment };

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
    // A same-document hash link (the skip-link's `#main`, chief among them) is
    // an in-page focus jump, not a navigation -- `url.pathname` resolves to the
    // CURRENT path for a bare `#main`, so `swappable()` alone can't tell the two
    // apart. Intercepting it would re-fetch and swap the page under the user's
    // feet instead of letting the browser move focus, and on a fetch failure
    // the catch fallback would turn "Skip to content" into a full page reload.
    if (url.pathname === window.location.pathname && url.hash) return;
    if (!swappable(url)) return;
    event.preventDefault();
    navigate(url.href);
  });

  window.addEventListener("popstate", () => {
    navigate(window.location.href, { push: false });
  });
}
