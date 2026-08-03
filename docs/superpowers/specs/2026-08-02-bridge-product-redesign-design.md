# Bridge Product Redesign

**Date:** 2026-08-02

**Status:** Approved

**Scope:** Full web-interface redesign for Bridge's existing local control-panel capabilities

## Goal

Make Bridge immediately understandable to a new user without flattening its operational depth into one oversized dashboard.

Bridge should answer three questions in order:

1. What needs my attention?
2. What project am I continuing?
3. What action will Bridge take when I continue it?

The redesign preserves Bridge's existing operational and safety contracts. It changes information architecture, visual hierarchy, and interaction composition. It does not replace the backend, add a frontend framework, or broaden Bridge's authority over project repositories.

## Approved Direction

Bridge becomes a calm, multi-page local tool with five primary navigation destinations. The Project workspace is nested under Projects:

- **Overview** — attention items, recent projects, and upcoming scheduled work.
- **Projects** — the complete searchable project index.
- **Schedule** — upcoming work, failures requiring recovery, and completed history.
- **Diagnostics** — runtime, indexing, and storage health with recovery guidance.
- **Settings** — browser-local preferences and deliberately read-only machine configuration.

Opening a project leads to its **Project workspace**, which contains Current work plus Sessions, Handoffs, and Launches tabs without adding another global navigation item.

The design is a quiet structural instrument, not a generic SaaS dashboard. It uses cool structural neutrals, a work-blue accent, a rust risk color, disciplined whitespace, and a functional session-to-handoff-to-next-session span as its signature element.

## Why the Current Interface Fails

The current dashboard renders every active project as a large card and gives blank session composition, current state, queued handoff, launch settings, git telemetry, and token telemetry comparable visual weight. In the live product, the first card is roughly one viewport tall. A user with dozens of projects cannot scan the portfolio without scrolling through controls they did not ask to use.

The problem is not missing capability. Bridge already exposes the right primitives:

- actionability sorting;
- live session state;
- freshness and explicit refresh;
- queued handoffs;
- launch model, effort, mode, and permission controls;
- scheduling and recovery;
- git risk and token telemetry;
- project, session, handoff, and launch history.

The redesign gives each primitive a place and a priority instead of adding more dashboard furniture.

## Product Principles

### One page, one job

Each destination has a single primary question. Deep data moves to a focused page or tab instead of expanding the landing page.

### Continuation before composition

When a project has a queued handoff, continuing that handoff is the primary action. Starting an unrelated session remains available, but it does not appear before the saved next step.

### Trust is always visible

Connection state, index freshness, async progress, completion, and recovery guidance are explicit. Bridge never presents stale state as current or allows a routine action to fail silently.

### Progressive disclosure stops at two levels

Primary actions and ordinary defaults stay visible. Advanced launch options and secondary project actions use one labeled disclosure level. Nothing important is buried behind nested panels.

### Stable content over clever motion

Updates patch text, attributes, and existing-node order. They do not replace authored fields, reload the page, or animate the interface for decoration.

### Honest authority boundaries

Bridge remains localhost-only, read-only toward project repositories, and sole writer of its own state. Settings never imply that Bridge can safely rewrite machine configuration when no such contract exists.

## Information Architecture

### Global shell

The shared shell contains:

- a Bridge wordmark and structural bridge-span mark;
- primary navigation;
- connection and freshness status;
- the page heading and page-specific actions;
- one main landmark;
- a skip link as the first focusable control.

Large screens use a persistent 208-pixel labeled sidebar. Medium and narrow screens use a Bridge header with a labeled **Menu** disclosure that reveals the complete navigation. Navigation never becomes unexplained icon-only controls.

The main canvas has a maximum readable width instead of stretching across every pixel of a 27-inch display. Extra width becomes whitespace or supports a purposeful secondary column; it does not trigger more widgets.

### Routes

| Destination | Route | V1 behavior |
|---|---|---|
| Overview | `/` | Recompose the existing dashboard read model. |
| Projects | `/projects` | New read-only HTML route using the existing project/card projection. |
| Project workspace | `/project/{project_id}?tab=current|sessions|handoffs|launches` | Recompose the existing route and add server-rendered tabs. |
| Schedule | `/schedule?view=upcoming|history&page=N` | New HTML route using existing schedule queries and mutations. |
| Diagnostics | `/diagnostics` | Recompose the existing diagnostic route. |
| Settings | `/settings` | New bounded route for browser preferences and effective read-only machine configuration. |

Unknown tab/view values return the destination's default view rather than a broken or blank surface. Invalid project IDs retain the current not-found behavior.

## Page Specifications

### Overview

**Job:** Answer “What needs my attention?”

The page contains, in order:

1. Page heading, plain-language attention summary, freshness, and Refresh.
2. **Needs attention** — running work, queued handoffs, failed or indeterminate scheduled runs, and stale repositories.
3. **Recent projects** — a short list of recently active projects.
4. **Up next** — the next few pending scheduled runs.
5. A quiet usage summary with absolute values only.

The page does not contain prompt textareas, launch selectors, project history tables, the full project catalog, or completed schedule history.

Each attention item has one context-sensitive primary action:

- queued handoff → **Continue in Terminal**;
- currently running → **Open project**;
- scheduled failure → **Review scheduled run**;
- stale repository → **Review project state**;
- ordinary recent project → **Open project**.

The server's existing priority contract remains authoritative: pinned, queued handoff, running, dirty/stale, recent, then idle. The Overview shows only the highest-value subset; **View all projects** goes to `/projects`.

Freshness has four visible states: connected, reconnecting, stale, and unavailable. It includes a state word, age, and recovery action where applicable. The state is never conveyed by color alone.

### Projects

**Job:** Find and open any project quickly.

The Projects page contains:

- a visible search field labeled **Search projects**;
- filters for All, Needs attention, Running, Queued, and Hidden;
- a stable list following the existing actionability order;
- project name and path;
- live/queued state in words;
- branch and dirty/ahead summary;
- last session title and age;
- a single **Open project** row action.

Search matches project name and path locally against the rendered/current projection. Filters update the result count and expose an explicit empty state.

Pin, Hide, and Restore are secondary row actions in a labeled menu or the Hidden view. Pinning and restoring use the current project mutation endpoint and refresh the existing read model without reloading or discarding input elsewhere.

The Projects page does not contain prompt composition, launch settings, or history tables.

### Project workspace

**Job:** Understand and continue one project.

The page header contains:

- Projects / project breadcrumb;
- project name and full path;
- Pin and Hide as explicit project actions;
- current tab navigation.

The four server-rendered tabs are:

- **Current work**
- **Sessions**
- **Handoffs**
- **Launches**

The selected tab is represented in the URL and with `aria-current="page"`. A full page load preserves predictable browser Back behavior and avoids introducing client-side routing.

#### Current work

When a queued handoff exists, the main surface contains:

- handoff summary and age;
- the real relationship span: Session ended → Handoff ready → Next session;
- a concise saved-prompt preview;
- **Edit prompt**;
- **Continue in Terminal** as the only primary button;
- **Schedule** as a secondary action;
- a labeled **Change options** disclosure for model, effort, mode, and permission.

Launch options show their effective values before launch. Permission always defaults to **Ask as usual** and is never persisted or pre-armed by a handoff. `bypassPermissions` retains conspicuous wording and risk treatment.

**Start a different session** is a separate secondary section. Opening it reveals one labeled prompt field and the same launch-options component. It does not replace or clear the queued handoff.

The handoff lifecycle action is named **Dismiss handoff**, matching the existing `dismissed` state. **Archive** remains reserved for project lifecycle semantics.

When no queued handoff exists, the page replaces the handoff surface with a short empty state and makes **Start session** the primary action.

A secondary Project state panel may show live session state, branch, dirty/ahead/behind, remote state, token totals, and freshness. It never competes visually with the next-session action.

#### Sessions, Handoffs, and Launches

V1 tabs retain the current honest boundary: up to the 50 most recent rows, newest first. Each tab says that limit explicitly.

Tables use:

- sticky opaque headers inside a labeled horizontal-scroll region when necessary;
- left-aligned prose, right-aligned numbers, and tabular numerals;
- visible empty states;
- status words in addition to color;
- no sortable affordance until sorting is actually implemented.

Full counts, pagination, and table filters are deferred until the store exposes consistent count/offset read contracts for all three histories.

### Schedule

**Job:** Plan, recover, and review scheduled runs.

The default **Upcoming** view groups:

1. failures, missed runs, and indeterminate runs requiring attention;
2. pending runs in chronological agenda order;
3. currently launching runs.

The separate **History** view shows terminal runs with server-side pagination. A calendar month grid is deliberately excluded: Bridge schedules a small number of exact developer sessions, and empty calendar days would consume space without improving the task.

Existing actions remain available with the same server contracts:

- Edit time
- Cancel
- Run now
- Retry

Each async action defines disabled/loading, success, error, and recovery states. Focus moves to the control that replaces a removed action or to the section heading/summary when no replacement exists. Cancelled rows leave the active agenda without making confirmation disappear.

The Overview schedule preview and full Schedule page use a shared server-rendered row/macro contract so state names, accessible labels, and JavaScript hooks cannot drift.

### Diagnostics

**Job:** Explain Bridge health and how to recover.

Diagnostics preserves the existing facts but groups them into:

- **Needs attention** — only failing or degraded checks, each with cause and next action;
- **Runtime** — liveness source, running sessions, and Claude version;
- **Indexing** — last run, files seen/scanned, parsed lines, duration, and errors;
- **Storage** — queued handoffs and spool depth.

When nothing is wrong, the page says **Bridge is healthy** and keeps routine facts visually quiet. It uses one page-level heading and the shared shell.

### Settings

**Job:** Make browser-local preferences clear and explain effective machine configuration without pretending Bridge can safely rewrite it.

V1 Settings contains:

- Appearance: System, Light, or Dark.
- Density: Comfortable or Compact, token-driven and never below accessible control targets.
- Safe launch defaults: model, effort, and terminal/background mode stored browser-locally.
- Permission behavior: read-only statement that permissions always begin at **Ask as usual**.
- Effective configuration: config-file path, project/session metadata directories, stale threshold, aliases, archived paths, database path, and port.
- Hook status and explicit setup/recovery instructions.

Appearance, density, and safe launch defaults persist in browser storage. Machine configuration is read-only in V1. Bridge does not rewrite `config.toml`, preserve/transform TOML comments, or mutate Claude settings through this page.

A future editable machine-settings feature requires its own design for validation, atomic persistence, unknown/comment preservation, reindex behavior, failure recovery, and mutation APIs. It is not smuggled into this visual redesign.

## Visual System

### Aesthetic

The aesthetic is **structural calm**:

- cool paper and steel-like surfaces;
- strong but quiet ink;
- work blue for focus, links, active navigation, and ready/working state;
- rust for risk, failure, and dangerous permission state only;
- thin structural rules and a small number of grounded surfaces;
- no gradient, glassmorphism, purple AI palette, decorative glow, emoji iconography, hero section, or bento grid.

The bridge-span mark and the session → handoff → next-session line are the distinctive elements. The relationship line appears only when those real states exist; it is never decorative progress theater.

### Token architecture

CSS uses three layers:

1. Primitive values for color, spacing, type, radius, and timing.
2. Semantic aliases for canvas, surface, text, work, risk, focus, and state.
3. Component tokens for navigation, buttons, fields, status pills, rows, tables, and handoff surfaces.

Components never contain raw colors. Theme changes swap semantic tokens rather than override individual components.

All color literals remain six-digit lowercase hex values so the existing contrast parser continues to test the real palette.

#### Light theme primitives

| Role | Value |
|---|---|
| Navigation | `#152125` |
| Canvas | `#f4f6f5` |
| Surface | `#ffffff` |
| Primary text | `#182427` |
| Secondary text | `#657378` |
| Rule/border | `#d9e1df` |
| Work | `#176579` |
| Work soft | `#dcecef` |
| Risk | `#a64f2f` |
| Risk soft | `#f6e6df` |

#### Dark theme primitives

| Role | Value |
|---|---|
| Navigation | `#0f191c` |
| Canvas | `#12191b` |
| Surface | `#192326` |
| Raised surface | `#202d30` |
| Primary text | `#eef3f2` |
| Secondary text | `#a9b7b8` |
| Rule/border | `#38474a` |
| Work | `#77b4c0` |
| Work soft | `#213e44` |
| Risk | `#e19a7e` |
| Risk soft | `#402820` |

Every foreground/background pair and every visible control boundary must pass the existing automated contrast gates before use.

### Typography

The interface uses at most two bundled, open-source families:

- **Atkinson Hyperlegible Next** for headings, navigation, prose, labels, and controls.
- **IBM Plex Mono** for paths, branches, IDs, times, token values, and authored prompt previews.

Fonts are served locally with `font-display: swap`; Bridge never depends on a public font CDN. Platform/system fallbacks keep content usable if a font asset fails.

The scale is tokenized. Body copy is 16 pixels by default. Dense metadata may use 12–14 pixels when contrast and line height remain sufficient. Long prose stays within roughly 65–75 characters per line.

### Spacing and shape

- 4-pixel primitive spacing base.
- Section gaps use 24, 32, or 40 pixels.
- Component gaps use 8, 12, or 16 pixels.
- Default control height is 40 pixels on pointer-oriented layouts and at least 44 pixels on touch/narrow layouts.
- Radii use 6, 8, 10, or 12 pixels. Pills are reserved for short states.
- Permanent surfaces use borders, not floating shadows. Shadow/elevation is reserved for menus, disclosures, and dialogs.

### Icons

All structural icons come from one consistent outlined SVG family with approximately 1.5–1.75 pixel stroke weight. Navigation always pairs icons with visible labels at the labeled sidebar and menu levels. Status icons have accessible text equivalents.

## Interaction States

Every button, link, row action, disclosure, field, tab, and menu defines:

- default;
- hover;
- pressed;
- focus-visible;
- disabled;
- loading;
- success;
- error.

Interaction color, border, and shadow transitions take 150–200 milliseconds. No transition animates layout dimensions or delays input. Reduced-motion mode removes translation and scale; opacity/color feedback remains.

Routine async actions provide visible feedback within 100 milliseconds. Buttons retain their action label while adding a progress state so the interface does not jump. Errors state the cause and recovery action next to the relevant control.

## Responsive Behavior

The layout is content-driven and tested at 320, 375, 768, 1024, 1440, and 2560 CSS pixels.

- **Large:** labeled sidebar, bounded content canvas, optional secondary column.
- **Medium:** labeled Menu disclosure and one- or two-column content according to available measure.
- **Narrow:** labeled Menu disclosure, single-column content, 44-pixel targets, no fixed side rail.
- **Tables:** semantic horizontal-scroll region only when a table genuinely cannot reflow.

No viewport requires horizontal scrolling to read prose or reach controls. At 200 percent text zoom, content reflows without clipping, overlapping, or hiding focused controls behind sticky chrome.

## Accessibility Contract

- WCAG 2.2 AA contrast for text and controls in light and dark themes.
- One logical page-level `h1`; sequential heading hierarchy.
- Skip link, landmark structure, and source order matching visual order.
- Visible two-pixel focus ring with offset in every theme.
- Native controls wherever possible.
- Visible labels for every input.
- State never communicated by color alone.
- Meaningful live-region announcements only for action/state transitions, not heartbeats.
- Route and tab changes place focus at the new page heading when browser navigation does not already provide an equivalent reset.
- Disclosures expose `aria-expanded` and `aria-controls`.
- Selected navigation and tabs expose `aria-current`.
- Empty, loading, partial, error, success, and unavailable states exist for every data-backed view.
- Reduced-motion preference is respected.

## Data and Implementation Architecture

Bridge remains server-rendered Jinja with progressively enhanced JavaScript. No SPA framework is introduced.

### Shared read models

The redesign should introduce focused read-model helpers rather than let templates assemble unrelated backend records:

- **Overview model:** attention items, recent projects, upcoming schedule, totals, diagnostics alert, and freshness.
- **Projects model:** complete project summaries plus filter counts.
- **Project workspace model:** current project summary, live state, queued handoff, launch options, git state, usage, and selected history tab.
- **Schedule model:** attention runs, upcoming runs, paged history, and total counts.
- **Settings model:** browser preference defaults plus effective read-only configuration and hook status.

The workspace helper reuses the same project/card sources as Overview and Projects. It must not re-probe git or live state through a divergent path.

### Shared components

Server-rendered Jinja macros/partials define:

- app navigation;
- freshness/connection status;
- project summary row;
- status pill;
- launch options;
- schedule row;
- empty/error state;
- history table shell.

Existing `data-*` hooks stay stable until their JavaScript and tests migrate in the same task.

### Live updates and preservation

Live updates may:

- patch leaf `textContent` and safe attributes;
- update status classes;
- add/remove hidden states on already-rendered shells;
- move existing project row/card nodes using the server-provided order.

Live updates may not:

- assign `innerHTML` from server data;
- replace a project workspace, prompt field, or handoff textarea;
- reload the page after pin, hide, restore, refresh, save, launch, or schedule actions;
- discard an authored prompt;
- reorder a focused project row without deferring the move or announcing it non-urgently.

## Safety Boundaries

- Bridge binds to localhost and has no remote authentication in this scope.
- Git remains read-only.
- Project repositories are never modified by the web UI.
- Permission mode defaults are never sticky.
- Dangerous permission choices remain explicit and visually distinct.
- Scheduled-run and handoff durability behavior is unchanged.
- Machine configuration is read-only in V1 Settings.
- No new write API is introduced for the redesign.

## Verification Requirements

### Automated

- Preserve and extend the full existing Python and JavaScript test suite.
- Add route/template tests for `/projects`, `/schedule`, `/settings`, and project tabs.
- Add tests for every empty and partial state.
- Extend contrast tests for every new semantic light/dark pair and control boundary.
- Prove selected navigation/tab semantics and heading order.
- Prove pin/hide/restore, prompt editing, launch, scheduling, retry, cancellation, and Refresh do not reload the page.
- Prove live updates preserve prompt node identity and authored values.
- Prove permission mode begins at **Ask as usual** regardless of stored browser defaults.
- Prove schedule preview and full schedule rows share status vocabulary and accessible names.

### Browser verification

Verify light and dark themes at:

- 2560×1440;
- 1440×900;
- 1024×768;
- 768×1024;
- 375×812;
- 320-pixel reflow equivalent;
- 200 percent text zoom.

For each critical page, verify keyboard-only operation, focus visibility, screen-reader names/states, reduced motion, long project names/paths, empty data, partial data, connection loss, async failure, and dangerous permission selection.

## Migration and Delivery Boundaries

The redesign can land as independently reviewable slices while keeping Bridge usable after each merge:

1. Shared tokens, typography, shell, navigation, and component states.
2. Calm Overview and Projects index.
3. Focused Project workspace and server-rendered history tabs.
4. Schedule and Diagnostics recomposition.
5. Bounded Settings, responsive/accessibility hardening, and final visual verification.

No slice may leave navigation pointing at a placeholder page. A destination appears in the shared shell only when its route is functional and tested.

## Deferred Work

- Editable machine configuration.
- Full project-history counts, pagination, filtering, and sorting.
- Global keyboard command palette and shortcuts.
- New activity-event storage.
- New status taxonomy.
- Remote access or authentication.
- Calendar-grid schedule view.
- Rich onboarding beyond honest empty states and setup guidance.

These are separate product features, not prerequisites for the approved redesign.
