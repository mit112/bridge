// The sidebar nav's Menu disclosure. Progressive: the nav is server-rendered
// fully visible with no `hidden` attribute, and this file does nothing until
// a click -- with no JS (or before this loads) the nav stays exactly as
// rendered, never collapsed out of reach.
(function () {
  if (!document.addEventListener) return;

  document.addEventListener("click", (event) => {
    const button = event.target && event.target.closest
      ? event.target.closest(".menu-toggle") : null;
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
