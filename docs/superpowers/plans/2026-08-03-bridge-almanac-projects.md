# Bridge Almanac Projects — Implementation Plan

**Spec:** `docs/superpowers/specs/2026-08-03-bridge-almanac-projects-design.md`
**Branch:** `feat/bridge-product-redesign`, from `417b90e` (979 passing)
**Goal:** Land the Almanac system's structural layer on `/projects`. The palette/type/masthead
already cascade; this pass fixes the row, the status treatment, the controls, the ragged action
column, and the never-designed 768–1250 band.

**Architecture:** Almost entirely CSS, scoped under a new `projects-index` class. Three small
`projects.html` edits and one `projects.js` edit. **`_components.html` is not touched** — a diff
against it means the containment strategy was abandoned (spec §7).

**Tech stack:** FastAPI + Jinja2, hand-authored `src/bridge/static/app.css`, vanilla per-page JS.
Tests: `uv run pytest` (never bare `pytest`).

## Global constraints

- **Locate CSS by selector, never line number.** `app.css` is ~1700 lines and every task shifts it.
- **`.projects-list` is shared with the Overview** (`overview.html:189`). Every new rule scopes under
  `.projects-index`. An unscoped `.projects-list` rule lands *after* `.overview-recent` in the sheet
  and wins on source order — silently restyling the Overview. This is the #1 failure mode.
- `tests/test_contrast.py` splits `app.css` at the **first literal** `prefers-color-scheme: dark`
  (app.css:131). Light primitives before, dark after; the substring must never appear earlier, not
  even in a comment. **No new colour pairs are expected** (spec §3.2) — if one is introduced anyway
  it MUST be added to `PAIRS` or it ships ungated.
- Components reference **semantic** tokens (`--run`/`--review`/`--work`/`--rule`), never `--p-*`.
- After any `.py` change: `lsof -t -iTCP:8787 | xargs kill` then `nohup uv run bridge serve &`
  (no `--port` flag; `lsof` is `/usr/sbin/lsof`). Restarting is pre-authorized. This pass changes no
  `.py`, so template/CSS/JS reloads suffice — but restart if anything looks stale.
- Measured baseline to beat (1440, dark): `actionSpread` **122px**, list **2930px**, rows 81–82px;
  1100px rows 81→108px; 768px rows 81→155px, list 3072px.

---

### Task 1: Scope hook, fixed action column, constant `Actions` label

**Files:** `src/bridge/templates/projects.html`, `src/bridge/static/app.css`,
`tests/test_projects_route.py`

**Interfaces:** adds `.projects-index`; `<summary>` visible text becomes `Actions`, project name
moves to `aria-label`.

**Steps:**
1. `projects.html:35` — `<ul class="projects-list projects-index" data-projects-list>`.
2. `projects.html:43` — `<summary aria-label="Actions for {{ row.name }}">Actions</summary>`.
   The name must stay in the accessible name (spec §9.1.6): 36 summaries reading only "Actions" is
   an unusable screen-reader rotor.
3. In the Projects CSS block, add `.projects-index .project-row` with a **`ch`/`minmax`-based**
   action track (never a fixed px width — spec §9.1.7), and `.projects-index .projects-list__item`
   with its own action track. Both must be new `.projects-index`-scoped rules, not edits to the
   shared `.projects-list__item` base rule.
4. Give `.projects-index .projects-list__actions summary` and
   `.projects-index .project-row__action .btn` each ≥24×24px and ≥24px of mutual spacing
   (WCAG 2.5.8, spec §9.1.5) — padded independently of row height.
5. Test: assert the summary renders the constant text **and** the name-bearing `aria-label`.

**Acceptance:** `actionSpread == 0` measured at 1440/1280 (not eyeballed). Suite green.

---

### Task 2: Row typography and density

**Files:** `src/bridge/static/app.css`

**Interfaces:** none.

**Steps:**
1. `.projects-index .project-row__name` → `var(--font-display)` 600 `var(--text-title)` (20px,
   clears the 19px serif floor). Mirrors `.overview-recent .project-row__name` exactly so the two
   pages agree.
2. `.projects-index .project-row__path` → **wraps, does not truncate** (spec §9.1.9): remove
   `white-space: nowrap`/`text-overflow` in this scope, add `overflow-wrap: anywhere`. Keep mono,
   `--text-xs` (12px floor), `--text-secondary`.
3. Demote the metadata line by **weight and colour before size** (`vis-typo-weight-hierarchy`);
   `font-variant-numeric: tabular-nums` on the dirty count.
4. Tighten `.projects-index .project-row` padding from `var(--space-4)` toward `var(--space-3)` to
   reach ≈64px rows. Keep internal-vs-separating spacing at ≥1:2 (`vis-space-proximity`).
5. `.projects-index .project-row__action .btn` mirrors the Overview ghost: mono, `--text-xs`,
   `var(--work)`, transparent, `→` via `::after`. **Note:** the arrow *does* enter the accessible
   name (`link "Open project →"`) — established last pass; that is accepted, not a bug.

**Acceptance:** list height ≈2300px at 1440 for 36 rows. Names computed 20px Fraunces. No path
ellipsis anywhere.

---

### Task 3: Status treatment — small-caps word, status edge, pin marker

**Files:** `src/bridge/static/app.css`

**Interfaces:** consumes the existing `[data-project-state]` on the `<li>`; no new markup.

**Steps:**
1. `.projects-index .pill` — strip `background`/`border`/`padding`; set mono, `--text-xs`,
   `letter-spacing: .06em`, and **`font-variant-caps: all-small-caps`** — *not*
   `text-transform: uppercase` (spec §9.1.3).
2. Colour the word per state, using semantic tokens, mapping inherited verbatim from `7a61923`:
   `running → var(--run)`, `stale → var(--review)`, `queued → var(--work)`,
   `recent`/`idle` → `var(--text-secondary)`.
3. Status edge on `.projects-index .projects-list__item[data-project-state="…"]` via
   `box-shadow: inset 3px 0 0` (not `border-left` — must not shift grid tracks between states).
   `idle` gets **no edge** (transparent), deliberately: a hairline on every quiet row re-adds the
   noise this removes.
4. **Add no row hover tint.** The hairline rule + status edge are already two separator channels and
   `ux-table-density` caps it there (spec §9.1.8). Hover stays on the action link only, with a
   named-property transition (never `transition: all`) inside a `prefers-reduced-motion` guard.
5. Pin marker: `.projects-index .projects-list__item:has([data-project-pin][aria-pressed="true"])`.
   Decorative only — the button's own `aria-pressed` is the programmatic signal. Must degrade to
   "no marker" where `:has()` is unsupported, never to a broken layout.

**Acceptance:** grayscale screenshot still distinguishes every status (the word carries it). No new
`PAIRS` entry needed — verify by running the suite, not by assertion.

---

### Task 4: Controls bar and index header band

**Files:** `src/bridge/templates/projects.html`, `src/bridge/static/app.css`

**Interfaces:** adds an `aria-hidden` column-header band above the list.

**Steps:**
1. Search field: keep the **visible** `<label>` (never placeholder-as-label). Restyle to a
   bottom-ruled input. Font-size ≥16px to stop iOS Safari zoom (`vis-typo-body-size`); keep
   `--control-min`.
2. Filter chips: mono, `--text-xs`, small caps via `font-variant-caps`; counts
   `font-variant-numeric: tabular-nums`. Pressed chip keeps the solid terracotta fill.
   **Do not touch `aria-pressed`** — `projects.js` reads it as the filter state.
3. Result count: mono, `--text-secondary`. Keep `role="status" aria-live="polite"` exactly.
4. Add the header band in `projects.html` immediately above the `<ul>`, `aria-hidden="true"`
   (the list is a `<ul>`, not a grid — announcing column names would be a false structural promise).
   Labels `PROJECT` / `STATUS & ACTIVITY`, on the same tracks as the rows.
5. Distinguish the band by **weight, letter-spacing, small caps — not by a rule of its own**
   (`ux-table-scannable`: "headers carry weight, not a ruled line"). The one hairline beneath it is
   the list's existing top border, so the band adds no net rule.

**Acceptance:** band tracks align with row tracks at 1440/1280. Suite green.

---

### Task 5: Responsive bands

**Files:** `src/bridge/static/app.css`

**Steps:**
1. **768–1023**: two tracks (identity | activity); the action cell moves to a second grid row
   spanning both, right-aligned, so every row shares one structure.
2. **≤767**: restyle the existing single-column stack; `Open →` and `Actions` inline on one line,
   still ≥24px apart.
3. Verify content reflows at **320 CSS px** with no horizontal scroll (`a11y-text-reflow`), and that
   **200% zoom** on 1440 (= 720 CSS px) lands cleanly in the ≤767 layer.

**Acceptance:** at 768 and 1100, row heights for single-line-path rows vary ≤2px (the Overview Fix 7
criterion). Rows with wrapped paths are legitimately taller — that is Task 2's accepted trade, not a
regression. Measure; do not eyeball.

---

### Task 6: Query-aware zero-results state

**Files:** `src/bridge/static/projects.js`, `src/bridge/templates/projects.html`,
`tests/test_static_js.py`

**Steps:**
1. When `shown === 0`, write a message echoing the query and offering a way out
   (`ux-search-zero-results`). When the query is empty but a filter matched nothing, say *that*
   instead — the two empties need different copy (`ux-state-empty`).
2. Touch **only** the empty-node text. Do not change `projectsMatchesFilter`, the `data-project-*`
   hooks, the pin/hide/restore handlers, or the no-reload contract. Read the file's header comment
   first.
3. Extend the existing node harness (`PROJECTS_FILTER_HARNESS`) to assert the echoed query, and keep
   its existing `fetchCalled === false` assertion passing.

**Acceptance:** node tests green; no fetch, no reload.

---

### Task 7: Verification gate

**Files:** none (verification only).

1. `uv run pytest` — green, ≥979 plus the new assertions.
2. Render `/projects` at **1440, 1280, 1100, 768, 390 × light and dark**. Theme switching requires
   setting `localStorage["bridge.appearance"]` and **reloading** — emulating `prefers-color-scheme`
   alone is ignored because the app pins `data-theme` at load. **Restore it to unset afterwards.**
   Screenshots must be written inside the repo. If `resize_page` fails with "Restore window to
   normal state", use `emulate` with a `viewport` string.
3. Measure, don't eyeball: `actionSpread`, row-height range, list height, computed name font.
4. **Overview cascade gate** — render `/` and confirm the Recent projects rows are unchanged
   (20px Fraunces names, ghost `Open project →`, no status edge, no small-caps word). `git diff` must
   show `_components.html` untouched.
5. Grayscale check: every status still distinguishable.
6. Confirm no `PAIRS` gap: if any new colour pair was introduced, it is in `PAIRS`.

---

## Self-review

**Spec coverage:** §3.3+§7.1→T1; §3.4+§4.3→T2; §3.2+§4.2+§4.4→T3; §5+§6→T4; §8→T5; §10a→T6; §10→T7.

**Placeholder scan:** no TBDs; every task names its files, selectors and acceptance measurement.

**Ordering:** T1 introduces `.projects-index`, which T2–T5 all consume — it must land first. T6 and
T7 depend on everything before them. T2–T5 are otherwise independent.

**Known risks:** (1) the shared `.projects-list` scope — mitigated by T1's dedicated class and T7's
cascade gate; (2) `:has()` support for the pin marker — degrades to no marker; (3) path wrapping
trades row-height uniformity for information integrity, which §9.1.9 accepts explicitly and T5's
acceptance criterion is written around.

**Deliberately out of scope** (spec §9.2, all flagged for Mit): promoting Pin out of the disclosure;
radiogroup semantics for the filter chips; the terracotta accent/status collision; the
`anti-ai-default-look` rating of the Almanac direction itself; the Overview's `01`/`02` numerals.
