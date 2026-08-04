// The Settings page's own browser-local preferences: appearance, density, and
// convenience launch defaults -- model, effort, and terminal/background launch
// mode.
//
// Everything here is scoped to `data-settings-*` elements ONLY. It never
// queries or sets `data-launch-*` anything; it only writes localStorage. The
// stored model/effort/mode defaults are read back by launch.js, which prefills
// the live launch band's model/effort selects and the schedule form's mode
// select. Permission is deliberately NOT one of these defaults: the live
// permission control (_launch.html / launch.js) always renders "Ask as usual"
// and reads its own `<select>` fresh on every click, per the CONTROLLER
// DECISION that permission mode is never persisted or pre-armed.
//
// Settings itself has no write API (spec: read-only page) -- every value here
// lives only in this browser's localStorage.

const APPEARANCE_KEY = "bridge.appearance";
const DENSITY_KEY = "bridge.density";
const LAUNCH_MODEL_KEY = "bridge.launch.model";
const LAUNCH_EFFORT_KEY = "bridge.launch.effort";
const LAUNCH_MODE_KEY = "bridge.launch.mode";

// "System" resolves against the OS preference at apply time rather than
// leaving the attribute unset, so the effective theme is always a concrete
// value the CSS's `[data-theme="light"|"dark"]` blocks already key off.
function effectiveTheme(pref) {
  if (pref === "light" || pref === "dark") return pref;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function applyTheme() {
  const pref = localStorage.getItem(APPEARANCE_KEY) || "system";
  document.documentElement.setAttribute("data-theme", effectiveTheme(pref));
}

function applyDensity() {
  const pref = localStorage.getItem(DENSITY_KEY) || "comfortable";
  if (pref === "compact") {
    document.documentElement.setAttribute("data-density", "compact");
  } else {
    document.documentElement.removeAttribute("data-density");
  }
}

applyTheme();
applyDensity();

// "System" tracks the OS live: a user sitting on this page while the OS
// switches modes sees it follow without a reload. Only fires when the stored
// preference is still "system" at the moment the OS actually changes -- a
// user who picked Light/Dark explicitly must not be overridden.
const darkMedia = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
if (darkMedia && darkMedia.addEventListener) {
  darkMedia.addEventListener("change", () => {
    if ((localStorage.getItem(APPEARANCE_KEY) || "system") === "system") applyTheme();
  });
}

// One delegated `change` listener for all five selects, registered once. A
// direct `el.addEventListener` would die with the node the moment the content
// region is swapped, leaving the controls looking fine and silently saving
// nothing.
const SELECT_KEYS = [
  ["[data-settings-theme]", APPEARANCE_KEY, applyTheme],
  ["[data-settings-density]", DENSITY_KEY, applyDensity],
  ["[data-settings-launch-model]", LAUNCH_MODEL_KEY, null],
  ["[data-settings-launch-effort]", LAUNCH_EFFORT_KEY, null],
  ["[data-settings-launch-mode]", LAUNCH_MODE_KEY, null],
];

document.addEventListener("change", (event) => {
  if (!event.target || !event.target.closest) return;
  for (const [selector, key, onChange] of SELECT_KEYS) {
    const el = event.target.closest(selector);
    if (!el) continue;
    localStorage.setItem(key, el.value);
    if (onChange) onChange();
    return;
  }
});

// Restore stored values into the selects on every page view, leaving the
// server-rendered default alone where nothing is stored yet. This has to re-run
// after a swap: the selects are fresh nodes carrying the server's defaults, so
// without it Settings shows values the user did not choose.
function restoreSettingsSelects() {
  for (const [selector, key] of SELECT_KEYS) {
    const el = document.querySelector(selector);
    if (!el) continue;
    const stored = localStorage.getItem(key);
    if (stored !== null) el.value = stored;
  }
}

if (window.bridgePage) window.bridgePage.onEnter(restoreSettingsSelects);
else restoreSettingsSelects();
