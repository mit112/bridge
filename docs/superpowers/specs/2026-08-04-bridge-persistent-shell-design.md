# Bridge persistent shell — design

Stop replacing the document on every navigation. Swap only the content region, so the sidebar,
masthead chrome, scroll position, and the SSE connection are never torn down.

Companion ledgers: `.superpowers/sdd/2026-08-03-bridge-almanac-projects/progress.md` (the diagnosis
that led here) and `.superpowers/sdd/2026-08-03-bridge-almanac-overview/progress.md` (render recipes,
server-restart rules).

## 1. Why, and why not CSS

Three consecutive sessions attacked a reported "whole screen flashing, especially in light mode" at
the view-transition layer and failed. The correct diagnosis, reached only after measuring in Mit's
actual browser (Arc, via `osascript`, not the devtools Chrome):

**There is no animation defect. The jolt IS the instant whole-document replacement.** Light mode is
high-contrast dark-on-cream, so swapping one dense page for another is a large visual change; dark
mode's lower contrast mutes the same event. Mit's original phrase — "a UI trick we need to figure
out" — was the correct diagnosis from the beginning; he was describing the teardown, not a defect.

Two dead ends are established by measurement and must not be revisited:

- Any opacity cross-fade between two structurally unrelated documents is a **double exposure**, not a
  mistuning. Frames showed the Overview and the Projects ledger both fully legible, superimposed, for
  ~60ms.
- Dissolving through the page background flashes flat cream in light mode — the same 0.17-composite
  dip an earlier session already found and fixed, in a new costume.

Nothing at the CSS layer can answer "how do we stop the whole screen changing at once". This design
reverses a DON'T recommendation recorded twice in the ledger. The reversal is deliberate and is Mit's
explicit decision: that recommendation was answering "is this worth it to remove a smear", which is a
different question.

**The genuine, measurable gain beyond the visual one:** the SSE connection survives navigation.
`live.js:295,335` opens a fresh `EventSource("/events")` per document today.

## 2. Decisions taken, with their alternatives

| Decision | Chosen | Rejected, and why |
|---|---|---|
| Swap engine | Hand-rolled, ~80 lines | htmx / Turbo — reverses a twice-recorded decision, adds 50–90KB to a render-blocking path, and its lifecycle events become the thing the harness must model |
| Harness DOM | Layered; no new dependency | jsdom — real fidelity, but adds `package.json` + `node_modules` to a repo with a deliberate zero-build-step posture, and re-introduces a known skip-trap (below) |
| Swap scope | The five sidebar destinations only | All same-origin GETs; form posts — larger blast radius for no additional relief of the reported symptom |

**The skip-trap, recorded because it already cost a real bug:** `tools/falsify.py` runs pytest with
`PATH=/usr/bin:/bin`, where Homebrew's node is invisible. A bare `shutil.which("node")` therefore
skipped the JS module under falsification, pytest exited 0, and a mutation came back SURVIVED. **A
skipped test is indistinguishable from a passing one.** `tests/test_static_js.py:26–43` already
compensates by searching absolute node locations; every new node-backed test must do the same.

## 3. The seam

`base.html` already has the right shape. Its `.shell__body` wrapper contains five of the six Jinja
blocks a page can override:

```
<aside class="sidebar">          PERSISTS   nav, sidebar toggle, .shell-status
<div class="shell__body">        SWAPPED    .page-head (page_eyebrow, page_title, page_summary,
                                            page_actions) + <main> (content)
<script> x6                      PERSISTS   never re-executed (see §4)
```

Block audit across all six page templates confirms this:

| Block | Renders inside | Overridden by |
|---|---|---|
| `content`, `page_title`, `page_summary` | `.shell__body` | all six |
| `page_eyebrow`, `page_actions` | `.shell__body` | overview, project |
| `shell_status` | `<aside>` | overview, project |
| `title` | `<head>` | settings, project |
| `scripts` | end of `<body>` | **settings only** |

So the swap is **two DOM nodes** — `.shell__body` and `.shell-status` — plus `document.title` and the
nav's `aria-current`. Not the five separate targets the earlier evaluation estimated.

`{% block scripts %}` is eliminated rather than supported: it has exactly one user (`settings.html`),
and moving `settings.js` into the always-loaded set removes the entire script-injection path.

## 4. The init contract

**Scripts are loaded once and never re-executed.** This is forced, not preferred: `launch.js:8`,
`live.js:4,5,6,249,250,252,253,282,335` and `settings.js:17–21,54` declare top-level `const`, which
creates a global lexical binding. Re-evaluating any of those files in the same realm throws
`SyntaxError: Identifier ... has already been declared` and aborts the **entire file**. Any design
that re-injects script tags is dead on arrival.

Instead, a minimal registry in `shell.js` (loaded first):

```js
window.bridgePage = {
  onEnter(fn),   // after a swap, and once on first load
  onLeave(fn),   // BEFORE a swap — commits pending edits
};
```

Rules, each enforced by a static test in §5:

1. No module-scope DOM capture. Every element lookup happens inside a handler or an `onEnter` hook.
2. No `DOMContentLoaded` binding outside `shell.js`'s own bootstrap.
3. Anything that must run per-page-view registers via `onEnter`.
4. Delegated listeners stay registered on `document` at module scope and are never re-registered.

Rule 4 is why this is cheap: `copy.js`, `shell.js`, `projects.js`, `schedule.js` and `launch.js`
already delegate on `document`, so their listeners survive a swap **with no change at all**. The work
is confined to the run-once calls.

### Per-file changes

| File | Change |
|---|---|
| `shell.js` | Hosts the registry. Its own delegated toggle handler already survives; no behaviour change. |
| `copy.js` | None. Fully delegated, no module state. |
| `projects.js` | `applyProjectsFilter()` (:306) and `applyProjectsView(...)` (:312) become `onEnter` hooks. `window.location.assign("/projects")` (:111) routes through the router instead. |
| `schedule.js` | `DOMContentLoaded` binding (:188) deleted; `paintScheduledTimes()` becomes an `onEnter` hook. |
| `launch.js` | `prefillLaunchDefaults()` (:226–256) becomes an `onEnter` hook. **New `onLeave` hook flushes a focused, edited prompt** — see §6. |
| `live.js` | Boot block (:281–292) extracted to a callable `bootFreshness()`, registered as `onEnter`, which first resets `lastConnectionState = null` so the re-announce always writes to the newly-swapped strip. `initialStrip` (:282) stops being module-scope. The `EventSource` and the 1s interval are created once and deliberately left alone — surviving is the point. |
| `settings.js` | Moves from `{% block scripts %}` into base.html's always-loaded set. `bindSelect`'s captured `el` (:64) is replaced by delegated `change` handling. |

## 5. The harness — built and green BEFORE any router code

The current suite is structurally blind to every one of these: all 156 route tests render a full
document, and all 84 `tests/test_static_js.py` tests model exactly ONE document lifetime. Shipping the
router against today's suite means shipping a second render path and a swap lifecycle with no
regression net. This section is the whole risk.

### Layer 1 — static invariants (`tests/test_shell_contract.py`, pure Python)

Reads the JS sources and asserts the §4 rules directly. This is the honest workhorse: it enforces the
architectural rule rather than simulating its consequences, needs no runtime, and cannot be faked by
a stub.

- No `DOMContentLoaded` outside `shell.js`.
- No module-scope assignment from `document.querySelector`/`getElementById` in any file.
- Every file that has per-page-view behaviour registers at least one `bridgePage.onEnter`.
- `launch.js` registers an `onLeave`.
- No template overrides `{% block scripts %}` (the path is gone; nothing may reintroduce it).
- `settings.js` is referenced from `base.html`, not from a page template.

Written to FAIL against today's code first, proving each one bites, then the JS is changed to satisfy
them.

### Layer 2 — fragment contract (`tests/test_fragment_routes.py`, TestClient)

For each of the five sidebar destinations: request with the fragment header, assert the response
carries all four swap payloads (`.shell__body` markup, `.shell-status` markup, title, active nav key)
and that a request WITHOUT the header still returns the full document byte-for-byte unchanged. That
second assertion is what protects the 156 existing route tests from becoming a lie.

### Layer 3 — swap lifecycle (`tests/test_swap_lifecycle.py` → node subprocess)

Extends the existing precedent in `test_static_js.py`: real JS files under real node, with a
hand-rolled minimal DOM providing element identity, `querySelector`, delegated event dispatch with
bubbling, and counting stubs for `EventSource`/`setInterval`/`fetch`. Asserts across **two or more
document lifetimes**: enter → leave → enter.

- Exactly one `EventSource` after N navigations.
- Exactly one 1s interval after N navigations.
- One `POST /api/launch` per ▶ click after N navigations (the most dangerous duplicate).
- One `POST /api/schedule` per submit after N navigations.
- The freshness strip re-syncs after returning to Overview with a changed connection state.
- `onLeave` fires before the content is detached.

**Stated honestly, not overclaimed:** layer 3 has lower fidelity than a real DOM. It proves the
lifecycle contract — hook ordering, call counts, listener duplication — not layout, CSS, or true
browser event semantics. Layers 1 and 2 are what make that acceptable, because layer 1 forbids the
bug class outright rather than testing for its symptoms.

### Mutation testing

Every new test is verified with the repo's own `tools/falsify.py`. This repo has the habit and it keeps
paying: the last two passes caught a presence-only assertion that survived swapping `animation: none`
for a duration, and a selector regex that matched the *prose of a comment* rather than the rule. A
survivor means either a vacuous test or an untested invariant — both worth fixing.

## 6. The router

New `src/bridge/static/router.js`, loaded after `shell.js`. Roughly 80 lines.

1. Delegated `click` on `document`. Opts in only for the five sidebar destinations. Ignores anything
   with a modifier key, a non-left button, `target`, `download`, or a cross-origin href — the standard
   set, so ⌘-click still opens a tab.
2. `preventDefault`, run `onLeave` hooks, `fetch(url, {headers: {"X-Bridge-Fragment": "1"}})`.
3. Replace `.shell__body` and `.shell-status`, set `document.title`, move `aria-current`.
4. `history.pushState`; handle `popstate` symmetrically.
5. Run `onEnter` hooks.
6. Move focus to `<main>` and announce the new page title via a live region — a swap does not move
   focus or notify a screen reader on its own, and this is non-negotiable for WCAG.

**Failure is always a normal navigation.** Any throw, non-OK status, or absent JS falls back to
`window.location.assign(href)`. Progressive enhancement is what keeps the five routes honest: they
remain ordinary links that work with JS off.

Server side: the **five in-scope routes** gain fragment awareness in `src/bridge/api.py`. A request
carrying the fragment header renders the same template context through a fragment wrapper; everything
else is untouched, which is why the existing 156 route tests stay valid. `/project/{id}` deliberately
does NOT get fragment awareness — it is out of scope (§9), so it stays an ordinary document load and
the router never intercepts a link to it.

## 7. Consequences, including the ones that cost something

- **`@view-transition` and the named groups go inert.** With no cross-document navigation left, the
  rules landed in `b0eded1`/`bd1bb01` stop firing. **Do not delete them as part of this work** — delete
  them only once the router is verified in Arc, and note that same-document view transitions become
  available at that point. That is the right place to add motion back, deliberately, and it is out of
  scope here.
- **Speculation-rules prefetch (`base.html:82–89`) now duplicates the router's own fetch.** It
  prefetches full documents on hover; the router requests fragments. Resolve after the router is
  measured, not before — it may still be a net win for the first navigation.
- **`aria-current`, `<title>` and `shell_status` become client-managed**, having been server-rendered.
  That is new state to keep honest, and it is why they are named explicitly in layer 2.
- Two pre-existing bugs surfaced by the audit, neither swap-induced. Pinned by tests so they are not
  later misread as regressions: `schedule.js:87` queries `[data-scheduled-count]`, an attribute in no
  template, so `bumpScheduledCount` is already a full no-op; and `launch.js:10` / `schedule.js:8` both
  define a global `announce`, with schedule.js winning by load order.

## 8. Verification — in Arc, not the devtools Chrome

The single omission that cost two sessions: everything was measured in Chrome 150 while Mit was
looking at Arc, which advertises Chrome 151 but runs an older engine and was not executing the feature
being fixed at all.

Definition of done:

1. `uv run pytest` green, unpiped. (Piping masks the exit code — this shell is zsh, where
   `${PIPESTATUS[0]}` is empty, and a piped pytest in an `&&` chain has already landed two commits on
   a red suite in this repo.)
2. All new tests mutation-verified with no survivors.
3. **The Overview cascade gate**, re-measured in dark at 1440 and byte-identical to the baseline that
   has now passed five times: 5 rows all `offsetHeight` 79, `.project-row` grid
   `237.891px 321.164px 117.852px`, padding `12px 0px`, minHeight `60px`, name Fraunces 20px 600,
   path IBM Plex Mono 13px, pill bg `rgb(36,31,26)`, action `rgb(219,112,72)`. Compare `offsetHeight`
   to `offsetHeight` — `getBoundingClientRect` reports 78.5 for those same rows.
4. **Driven in Arc via `osascript`**, exercising all five destinations plus back/forward: sidebar never
   repaints, scroll position holds, one `EventSource` for the session, `/settings` still functional
   after arriving by swap, schedule times still local, the projects toggle announcing correctly.

## 9. Out of scope

`Cache-Control` on static assets (independent, tracked separately); deleting the view-transition CSS;
the speculation-rules resolution; form posts and `/project/{id}` navigations; anything on Mit's open
tinker list.
