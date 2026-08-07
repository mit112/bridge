# Whole-App Liveness — Design

**Date:** 2026-08-07
**Status:** Approved (design), pending implementation plan

## Problem

Bridge only reflects underlying changes live on the **Overview** page. Every
other surface — the project detail page (session list, agent status, git,
burn, history tables), `/schedule`, `/diagnostics`, `/settings` — is a static
server render. It updates only when the user navigates to it (a fragment swap)
or hard-reloads. The user must reload to see what changed. Since Bridge is
becoming an open-source project, the fix must be polished and follow the
standard server-rendered "HTML over the wire" liveness pattern, not a bespoke
per-page hack.

## What already exists (the backbone we reuse)

- **A persistent SSE stream.** `live.js` opens a single global
  `EventSource("/events")` at document load. All JS loads once in `base.html`;
  the router (`router.js`) swaps only `.shell__body` and never reloads the
  document, so the SSE connection survives every navigation. It is already a
  live pipe available to every surface.
- **Change-only frames.** `/events` emits a frame only when something changed.
  Every frame carries `generation` (incremented on each ~15s reindex by
  `RefreshCoordinator`) and per-card live status; agent-status changes tick at
  the ~3s poll cadence.
- **Fragment routes for every target surface.** `/project/{id}`, `/schedule`,
  `/diagnostics`, `/settings` all serve a fragment when requested with the
  `X-Bridge-Fragment: 1` header. "Refresh a surface" therefore already has a
  server endpoint.
- **A per-page lifecycle.** `window.bridgePage.onEnter(fn)` / `.onLeave(fn)`
  (in `shell.js`) run on each swap; `router.js` calls `enter()` after applying
  a fragment.

The consequence: this is almost entirely a **client-side extension**. Server
work is expected to be zero-to-minimal.

## Approach

**One signal, generic morph-refresh.** A new client controller watches the
existing SSE stream. When a change relevant to the *current route* arrives, it
silently re-fetches the current page's own fragment (same URL,
`X-Bridge-Fragment: 1`) and **morphs** the new HTML into `.shell__body` instead
of the router's blunt `replaceWith`.

Morphing is the core decision. A DOM morph diffs the incoming server HTML
against the live DOM and mutates only the nodes that actually changed, so
scroll position, focus, `<details>` expand/collapse state, and in-flight form
input are preserved automatically. A full `replaceWith` (what the router does
on intentional navigation) would destroy all of that on every background tick —
unacceptable for an update the user did not ask for. This is the standard
htmx/Turbo "HTML over the wire" pattern.

**Vendor idiomorph** (single MIT-licensed file, small) as `static/vendor/
idiomorph.js`, rather than hand-rolling a DOM differ. It is the battle-tested
standard for this exact problem and is less code for the project to own and
maintain — the correct "small dependency" tier for an OSS project.

**Overview stays surgical.** `live.js` already patches Overview leaves at ~3s
granularity and carries many documented, tested bug fixes. It is kept and
*extended*, not rewritten. The morph-refresh controller covers the four other
swappable surfaces only. (Unifying Overview onto the morph path is a real
consistency win but a riskier rewrite; it is logged as a documented follow-up,
explicitly out of scope here.)

## Components

### 1. `static/vendor/idiomorph.js` (new, vendored)
The morph library, unmodified, with its license header intact. Exposed as
`Idiomorph` for the controller.

### 2. `static/liverefresh.js` (new)
The controller. Responsibilities, each independently testable:

- **Subscribe to change signals.** On load, register a listener that receives
  the same SSE frames `live.js` parses. Rather than opening a second
  `EventSource`, `live.js` exposes a small fan-out (e.g.
  `window.bridgeLive.onFrame(fn)`) so both consumers share the one connection.
- **Decide relevance per route.** The controller only acts when the current
  route is one it owns (`/project/{id}`, `/schedule`, `/diagnostics`,
  `/settings`) AND the frame indicates a change that surface could reflect:
  - a `generation` increase (covers all reindex-derived content — sessions,
    git, burn, history, schedule rows, diagnostics counters), or
  - for `/project/{id}`, a change to that project's own live status.
- **Refresh, debounced and safe.** On a relevant signal, fetch the current
  URL's fragment and morph it into `.shell__body`. Coalesce bursts (a short
  debounce) so a flurry of frames yields one refresh.
- **Defer when unsafe.** Skip (and retry on the next idle signal) while the
  user has unsaved handoff text or focus inside `.shell__body`.
- **Reset on navigation.** Register via `bridgePage.onEnter` so the current
  route and its baseline `generation` are re-read after each swap;
  `onLeave` cancels any pending refresh.

### 3. Volatile-node markup (template change, minimal)
Nodes a background morph must never clobber are marked so the morph steps over
them:

- The **handoff textarea(s)** — user may be typing; the server value must not
  overwrite in-flight input. Already treated as an identity boundary by
  `live.js`.
- Any **focused input** and **open menu / popover** — handled at morph time via
  idiomorph's ignore callback keyed on `document.activeElement` and an
  `[data-live-preserve]` attribute for statically-known volatile nodes.

### 4. `live.js` — small extension (no rewrite)
Add the `onFrame` fan-out so `liverefresh.js` shares the connection. No change
to Overview patching behavior.

### 5. Feel-alive polish (small, in-scope)
- A brief, `prefers-reduced-motion`-respecting highlight on a value that just
  changed, so the user perceives the update.
- The existing connection-state strip ("Live / Reconnecting / Stale") is
  surfaced consistently on the morphed surfaces so the user can trust the page
  is live (or know when it isn't).

## Data flow

```
RefreshCoordinator (reindex ~15s)   agents.probe (status ~3s)
                 \                   /
                  v                 v
             /events SSE  ──(change-only frame: generation, per-card live)──┐
                                                                            v
                                                       live.js EventSource (persistent)
                                                          |                    |
                              (Overview surgical patch)   |   onFrame fan-out  |
                                                          v                    v
                                                     Overview DOM      liverefresh.js
                                                                             |
                                              relevant to current route? ----+
                                                    | yes (debounced, safe)
                                                    v
                              fetch(currentURL, X-Bridge-Fragment:1) -> morph .shell__body
                                            (preserving scroll/focus/input/expand state)
```

## Error handling

- **Fetch fails / non-OK:** keep the existing DOM untouched; the surface simply
  does not update this tick. No fallback full reload (unlike navigation, a
  background refresh has no user intent to honor — silently keeping stale-but-
  intact content is correct). Retry on the next signal.
- **Malformed / unusable fragment:** same — keep current DOM, log to console,
  retry next signal.
- **Morph library absent (defensive):** controller no-ops; surfaces behave
  exactly as today (update on navigation only). Progressive enhancement is
  preserved.
- **SSE disconnected:** unchanged from today — `live.js` already handles
  reconnect/backoff and connection-state reporting; the controller just stops
  receiving frames until it reconnects.

## Testing

- **Controller unit tests (JS harness):** feed mock frames and assert:
  refresh triggered on relevant `generation` bump; not triggered on irrelevant
  route; per-project live change triggers on `/project/{id}`; burst coalesces
  to one refresh; refresh deferred while a volatile node is focused / handoff
  is dirty, then runs on the next signal; navigation resets baseline.
- **Morph preserve tests:** morph HTML with a changed sibling and assert a
  focused input's value and an open `<details>` survive; assert a
  `[data-live-preserve]` node is untouched.
- **Server:** existing fragment-route tests cover the endpoints; add a test
  only if a signal tweak proves necessary.
- Full existing suite (pytest + JS) must stay green.

## Out of scope (documented follow-ups)

- Unifying Overview onto the morph path.
- Any new server-push transport (WebSockets); the existing SSE + fragment model
  is sufficient.
- Live updates to surfaces that are not swappable routes.
