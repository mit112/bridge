# Bridge UI/UX audit

**Audited:** 2026-08-02

**Baseline:** `822635b` (`main`; 721 tests collected)

**Product question:** Does the panel show trustworthy project state at a glance and make the next session one click away?

## Verdict

Bridge has the right information model and the right actionability sort, but the rendered product does not yet meet its own goal when a tab stays open for days. The largest gap is not cosmetic: most of the state presented as current is a load-time snapshot with no age or connection warning. The next-largest gap is that Bridge exposes two “Run now” paths with different launch settings and different failure recovery.

The underlying card order, single-column/two-column breakpoint, absolute token counts, localhost boundary, and sole-writer architecture are sound. This pass should preserve them.

## Ranked findings

| Rank | Type | Finding | Evidence | KB rule(s) | Proposed fix |
|---|---|---|---|---|---|
| P0 | Defect | A long-lived tab presents stale topbar totals, git state, burn, sparklines, diagnostics, and card order as current. Only the live-status word changes. | `src/bridge/templates/dashboard.html:3-20`; `src/bridge/templates/_card.html:100-121,228-242`; `src/bridge/static/live.js:27-43`; `src/bridge/cards.py:238,272-301` | `fnd-heuristic-visibility-status`, `ux-state-design-all`, `a11y-live-regions` | Add an explicit data-freshness/connection strip, periodic server-process reindexing, a structured dashboard snapshot, leaf-node patching, and safe movement of existing card `<li>` nodes. Never replace a card or handoff textarea. |
| P0 | Defect | Reindexing exists but cannot be initiated from the UI, and reindexing alone would not update an already-open page. | `src/bridge/api.py:835-837`; no caller in `src/bridge/templates/` or `src/bridge/static/` | `fnd-affordance-signifier`, `ux-feedback-immediate`, `ux-state-error-recovery` | Add a labeled Refresh action with immediate/persistent status. After the POST, consume the same structured snapshot used by the periodic path; do not reload the page. |
| P0 | Defect | The compose-box “Run now” ignores model, effort, and permission, unlike the launch band; its failed-launch paths also do not copy the prompt. | `src/bridge/static/schedule.js:188-221` versus `src/bridge/static/launch.js:44-128` | `fnd-heuristic-consistency-standards`, `ux-form-actions`, `ux-state-error-recovery` | Use one launch request builder and one failure formatter for both entry points. Read the card’s selected model/effort/permission and use `bridgeCopy` for every failed launch while keeping the prompt in place. |
| P0 | Defect | Pin, Restore, and some status copy explicitly tell the user to reload, leaving the visible state/order inconsistent with the saved state. | `src/bridge/static/projects.js:67-81,108-117` | `ux-feedback-immediate`, `ux-state-optimistic`, `insp-linear` | Move existing card nodes using the server-provided sort key after pinning. For restore, request one server-rendered card fragment or include a hidden inert card shell; do not duplicate `_card.html` in JavaScript. Preserve textarea nodes and values. |
| P1 | Defect | The flagship live band has no CSS, and live updates change the word without changing the parent’s state class. | `src/bridge/templates/_card.html:83-98`; `src/bridge/static/live.js:27-43`; no `.live*` rule in `src/bridge/static/app.css` | `vis-color-not-sole-signal`, `ux-feedback-immediate`, `a11y-color-not-sole` | Style the band as a compact telemetry line. Use the single accent only for actively running/working states; keep idle/ended/unknown neutral and waiting/error states word-and-glyph legible. Update the existing parent class without replacing markup. |
| P1 | Defect | SSE connection health, reconnect/backoff, last trustworthy update, and stale-data state are invisible. A disconnected tab looks healthy indefinitely. | `src/bridge/static/live.js:45-97`; no connection-state markup in `base.html` or `dashboard.html` | KB gap; nearest rules: `fnd-heuristic-visibility-status`, `ux-state-error-recovery`, `a11y-live-regions` | Add a persistent connection/freshness label that distinguishes connected, reconnecting, stale, and unavailable. Announce only meaningful transitions, not every heartbeat. A proposal was added to the KB inbox. |
| P1 | Defect | Several shipped classes are completely unstyled, leaving diagnostics and parts of detail/schedule views visually unrelated to the dashboard. | `.muted`, `.spark`, `.scheduled__project`, `.sessions__changes`, `.diag`, `.status--*` in `src/bridge/templates/`; absent from `src/bridge/static/app.css:1-391` except `.status--queued` | `vis-hier-visual-hierarchy`, `ux-table-scannable`, `vis-typo-tabular-nums` | Add component rules using the same semantic tokens, density, numeric treatment, and warning restraint as cards. Do not introduce new status hues. |
| P1 | Defect | The stylesheet has no spacing/type/control scale and uses many one-off values; similar controls and list rows are repeated with near-identical rules. | `src/bridge/static/app.css:31-79,102-184,207-236,240-267,273-390` | `sys-tokens-layers`, `sys-tokens-semantic-naming`, `vis-space-scale`, `anti-inconsistent-spacing`, `anti-too-many-fonts` | Introduce a compact 4px spacing scale, a small type scale, radius/control tokens, and shared form/table/list primitives. Keep every color as six-digit lowercase hex so contrast tests remain real. |
| P1 | Defect | The current green queued surface and red/green additions/deletions exceed the spec’s locked color vocabulary: running accent plus one risk warning. | `src/bridge/static/app.css:10-12,22-27,83-99,226-235` | `vis-color-palette-restraint`, `vis-color-semantic-roles`, `anti-rainbow-palette` | Make queued state neutral and prominent through border weight/spacing/type. Keep `+`/`-` text but render both neutrally. Reserve accent for live activity/focus/links and warning for risk/failure. |
| P1 | Defect | Narrow/zoomed layouts can overflow: the eight-item totals row cannot wrap, card-header controls compete with the title, and tables have no overflow strategy. | `src/bridge/static/app.css:38-50,57,74-79,207-214`; all page tables in `project.html` and `diagnostics.html` | `a11y-text-resize`, `a11y-focus-order`, `ux-table-sticky` | Let totals and headers wrap in DOM order, keep controls at least 24px, and put tables in semantic scroll containers with sticky opaque headers. Verify at 200% zoom and narrow width. |
| P1 | Defect | Project history is silently capped at 50 sessions, handoffs, and launches, with no count, paging, or path to older rows. | `src/bridge/store.py:386-394,439-447,528-536`; `src/bridge/templates/project.html:22-108` | `anti-truncation-no-affordance`, `ux-table-pagination` | Add total counts and numbered or previous/next pagination per history table. Keep sort state explicit and stable. |
| P1 | Defect | “See the full history” navigates a person to raw JSON rather than a usable history view. | `src/bridge/templates/dashboard.html:130-140`; `src/bridge/api.py:1046-1058` | `anti-truncation-no-affordance`, `ux-table-pagination`, `fnd-heuristic-match-real-world` | Add an HTML scheduled-history route with status filter and pagination; keep the JSON API for clients. This is an information-architecture change and requires sequencing approval. |
| P1 | Defect | Empty and partial states are inconsistent: the detail page silently omits empty handoff/launch sections, and an empty sessions table has no explanation. | `src/bridge/templates/project.html:22-69,69-108` | `ux-state-design-all`, `ux-state-empty`, `a11y-semantic-structure` | Render labeled section headings and compact empty copy for history views. This does not add empty affordances to cards, preserving the locked card rule. |
| P1 | Defect | Diagnostics has two `<h1>` elements because `base.html` already supplies one, while project detail starts at `<h2>` and has no persistent route back to the panel. | `src/bridge/templates/base.html:10-21`; `src/bridge/templates/diagnostics.html:3`; `src/bridge/templates/project.html:4` | `a11y-semantic-structure`, `anti-mystery-meat-navigation` | Make the Bridge wordmark a labeled home link, keep exactly one page-level `<h1>`, and preserve logical heading order on every view. |
| P1 | Defect | The pin control uses an emoji-only, non-universal icon with no persistent visible label. | `src/bridge/templates/_card.html:15-18` | `anti-ai-emoji-decoration`, `anti-mystery-meat-navigation`, `a11y-name-role-value` | Replace the emoji with visible “Pin” text and retain `aria-pressed`; express the pressed state through weight/border plus the accessibility state, not a new hue. |
| P1 | Defect | The same schedule form is written twice, and form controls/status helpers are duplicated across CSS and JavaScript. The visible compose-launch drift is already a consequence. | `src/bridge/templates/_card.html:55-70,157-173`; `src/bridge/static/app.css:175-184,325-334,382-390`; `src/bridge/static/launch.js:10-13`; `schedule.js:8-11`; `projects.js:27-30` | `fnd-heuristic-consistency-standards`, `sys-comp-api-consistency` | Extract one Jinja schedule-form macro, one shared form-control rule, and one shared `bridgeAnnounce` helper. Keep delegated listeners and their current DOM/focus contracts. |
| P2 | Defect | Several data hooks and a launch event listener are dead: they imply update behavior that does not exist. | `base.html:17`; `dashboard.html:137`; `_card.html:36`; `live.js:99-108` | `fnd-heuristic-minimalist-design`, `sys-comp-api-consistency` | Remove unused hooks/listener, or wire them only as part of an approved freshness implementation. Do not retain inert “future” contracts. |
| P2 | Defect | The browser requests `/favicon.ico` and receives 404; metadata does not describe or theme the installed long-lived panel. | `src/bridge/templates/base.html:3-8`; confirmed in `~/.bridge/serve.log` | `fnd-aesthetic-usability`, `web-perf-cls` | Add a tiny static SVG favicon, a concrete description, and per-theme six-digit `theme-color` metadata. |
| P2 | Improvement | Theme follows only the OS. A manual override would help a window left open across changing light conditions, but it is not required by the approved spec. | `src/bridge/static/app.css:1-29` | `sys-theme-modes`, `sys-theme-token-driven` | After the token refactor, consider a labeled system/light/dark control persisted locally. Do not implement before the core freshness and launch fixes. |
| P2 | Improvement | Detail tables have no sort/filter controls, even where 50 rows are presented. | `src/bridge/templates/project.html:22-108` | `ux-table-sort-filter`, `ux-table-number-alignment` | Add visible table-local filters and accessible sortable headers only after paging/counts are designed; default to the server’s current newest-first order. |
| P2 | Improvement | Frequent expert actions have no keyboard accelerators beyond native Tab/Enter. | all templates/static scripts | `insp-linear`, `fnd-heuristic-flexibility-efficiency`, `a11y-keyboard-operable` | After the primary flow is trustworthy, add discoverable shortcuts for Refresh and focused-card Launch. Never make shortcuts the only path. |

## Cheap-win implementation lane

These changes are explicitly non-structural and can land before another design decision:

1. Establish semantic spacing/type/control/table tokens; style all shipped classes; fix narrow/zoom reflow, heading structure, the home link, metadata, and favicon.
2. Update live status words and parent classes together, add regression coverage, and remove the inert `bridge:launched` listener.
3. Give compose “Run now” launch-setting parity and the same clipboard fallback as the launch band, test-first.
4. Replace the pin emoji with a visible label; extract the duplicate schedule-form macro and shared announce/control primitives without changing behavior.
5. Add honest empty/cap copy to detail history while leaving full pagination and new routes to the structural lane.

## Structural lane — requires sequencing agreement

Recommended order:

1. **Freshness foundation:** connection/freshness strip, explicit Refresh, periodic reindex in the existing server process, dashboard-state JSON, leaf patching, and safe movement of existing card nodes.
2. **History integrity:** HTML scheduled history, counts, filters, and pagination for all capped histories.
3. **Optional efficiency:** keyboard accelerators and manual theme preference.

The freshness foundation is one coherent change. Shipping only a Refresh button would be misleading because the POST updates SQLite but not the open page; shipping only more SSE labels would still leave git, burn, diagnostics, and order stale.

## Visual direction for the cheap-win lane

Bridge is an expert local operations ledger, not a generic SaaS dashboard. The visual system should feel like a quiet instrument panel:

- **Color:** warm-neutral light/dark surfaces; one work-blue accent for links, focus, and active live state; one amber warning for risk/failure. Queued state is neutral.
- **Type:** Avenir Next for identity/headings, the platform UI face for prose/controls, and SFMono-compatible monospace for paths/data. Sizes come from one compact scale.
- **Density:** 4px base spacing, comfortable card reading with compact metadata/tables, 24px minimum web targets.
- **Signature:** the live-status line reads as telemetry — dot/glyph, explicit word, age, model/effort — while every other band stays quieter.

Anti-AI check: no indigo/violet default, no gradient, no glass, no hero/bento rewrite, no hype copy, no decorative emoji, and no animation without a state-change job. This direction is specific to a local session-control instrument and would not be reused unchanged for an unrelated product.

## Locked constraints reviewed

No locked constraint is wrong.

- Preserve pinned → queued handoff → running → dirty/stale → recent → idle.
- Preserve one column and two columns only at `min-width: 1400px`.
- Preserve absolute token counts; never add a gauge or percentage.
- Preserve localhost-only operation, read-only git, and the server-process sole-writer rule.
- Preserve handoff `<textarea>` identity and value. Live work may patch leaf `textContent`, attributes, and existing-node order only; it may not replace cards, assign `innerHTML`, or reload.
- Preserve six-digit lowercase hex color tokens unless the contrast parser is upgraded and deliberately-failing proof is added in the same commit.

## Status update — 2026-08-05 (de-AI / readability pass)

This audit's structural and cheap-win lanes shipped earlier under the product-redesign SDD. A later polish pass, driven by three owner complaints plus a "read less like generic AI" ask, shipped on branch `feat/bridge-deai-projects` (pushed @ `be66384`, 1169 passing, **not yet merged to main**). Three commits:

- `e6792b4` **Projects de-AI + readability.** Grouped, collapsible status sections (state carried by the group header's dot + label, so rows dropped their per-row pill and faint left edge); `stale` now labelled "Uncommitted"; Fraunces → **Young Serif** (OFL) display face; the `.page-head` Scotch double-rule → one hairline app-wide. Readability: the last-session note un-inked to muted so the serif name is the sole ink per row; the path left-truncated to its leaf (full value in `title`) instead of wrapping; sticky group headers.
- `aacee03` **De-tint status cards + nav.** Removed the coloured accent bar across the top of every card (Overview attention cards, Projects grid cards, the schedule empty-agenda panel) and the sidebar active-item's terracotta left bar — the accent-bar-on-a-panel was the generic-AI tell. Status colour now lives only on small labels/words, never a bar. Extends this audit's P1 palette-restraint finding.
- `be66384` **Launch double-selector dedup.** Resolves the remaining half of the P0 "two Run now paths / duplicate launch settings" finding: the no-handoff Current tab no longer renders a second model/effort/permission picker. The compose box carries its own picker only when collapsed (a handoff is queued and no project-keyed band exists); otherwise the single launch band owns the one picker and posts the compose prompt.

Still open (next session): vocabulary/token/voice unification across the other pages, a full colour-discipline sweep, and `<details>` collapse-state persistence across navigation.
