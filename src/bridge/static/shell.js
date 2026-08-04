// The sidebar nav's two disclosures. Progressive: the nav is server-rendered
// fully visible with no `hidden` attribute, and this file does nothing until
// a click -- with no JS (or before this loads) the nav stays exactly as
// rendered, never collapsed out of reach.
//
// `.menu-toggle` (below 1024px) hides the nav via the `hidden` attribute.
// `.sidebar-toggle` (1024px and up) collapses the whole rail via a `data-nav`
// attribute on <html> that CSS reads, and persists the choice -- Bridge is
// server-rendered, so every nav click is a full page load and an in-memory
// collapse would spring back open each time. base.html's inline head script
// applies the stored value before first paint; this file only handles the
// click and keeps the button's own state in step.
(function () {
  if (!document.addEventListener) return;

  const root = document.documentElement;

  // The server cannot know a client-only preference, so the button ships
  // `aria-expanded="true"` and is corrected here once the DOM exists. Safe to
  // run after paint: `data-nav` already did the visual work, and neither
  // `aria-expanded` nor the accessible name is a visual property, so there is
  // nothing here that could flash.
  function syncToggle() {
    const collapsed = root.getAttribute("data-nav") === "collapsed";
    const label = collapsed ? "Expand sidebar" : "Collapse sidebar";
    const buttons = document.querySelectorAll("[data-sidebar-toggle]");
    for (const button of buttons) {
      button.setAttribute("aria-expanded", collapsed ? "false" : "true");
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
    }
  }

  syncToggle();

  document.addEventListener("click", (event) => {
    if (!event.target || !event.target.closest) return;

    const sidebarToggle = event.target.closest("[data-sidebar-toggle]");
    if (sidebarToggle) {
      const collapsed = root.getAttribute("data-nav") === "collapsed";
      if (collapsed) {
        root.removeAttribute("data-nav");
      } else {
        root.setAttribute("data-nav", "collapsed");
      }
      try {
        localStorage.setItem("bridge.nav", collapsed ? "expanded" : "collapsed");
      } catch (e) {
        // A blocked or full localStorage must not cost the user the toggle
        // itself: the collapse still applies, it just will not outlive the
        // page. Silent because there is nothing the user could act on.
      }
      syncToggle();
      return;
    }

    const button = event.target.closest(".menu-toggle");
    if (!button) return;
    const nav = document.getElementById(button.getAttribute("aria-controls"));
    if (!nav) return;

    const expanded = button.getAttribute("aria-expanded") === "true";
    button.setAttribute("aria-expanded", expanded ? "false" : "true");
    if (expanded) {
      nav.setAttribute("hidden", "");
    } else {
      nav.removeAttribute("hidden");
    }
  });
})();
