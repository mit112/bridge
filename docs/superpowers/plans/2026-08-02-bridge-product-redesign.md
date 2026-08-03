# Bridge Product Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recompose Bridge's web interface into a calm, multi-page local tool (Overview, Projects, Project workspace, Schedule, Diagnostics, Settings) that answers "what needs my attention / what am I continuing / what will Bridge do" without flattening operational depth into one oversized dashboard — preserving every existing safety, storage, and progressive-enhancement contract.

**Architecture:** Server-rendered Jinja + progressively enhanced vanilla JS, unchanged. The redesign adds thin read-model helpers over the *existing* Store read API (no new queries, no new write APIs), a three-layer CSS token system, a shared app shell with sidebar navigation and server-rendered Jinja macros, and four new/recomposed HTML routes. The distinctive full-card interaction surface (compose / launch / handoff-edit / schedule) **moves from `/` into the Project workspace** and reuses the existing `data-*` hooks and JS verbatim so the node-executed JS tests stay green. Live SSE updates continue to patch only enumerated leaf nodes and to move whole card nodes via `append`.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, SQLite (via `bridge.store.Store`), vanilla ES (no bundler; tests `eval` each static `.js` under `node`), CSS custom properties, self-hosted woff2 fonts. Tests: pytest + FastAPI `TestClient` (in-process, no port bind). Mutation testing: `tools/falsify.py` + `tools/mutations/*.json`.

## Global Constraints

Every task's requirements implicitly include this section. Values are copied verbatim from `docs/superpowers/specs/2026-08-02-bridge-product-redesign-design.md`.

- **Baseline:** `uv run pytest` is 760 passing at HEAD `f371959` (the earlier "2 failed" was solely a `BRIDGE_PORT` env override breaking `test_config`'s default-port assertion; tests use in-process `TestClient` and never bind a port). Never weaken a test to make the redesign pass.
- **No SPA / framework.** Preserve server-rendered Jinja + progressive JS. No React, no client router. Full page loads drive route and tab changes.
- **Security/ownership unchanged.** Localhost-only, Git read-only, Bridge writes only its own `~/.bridge` state. **No new write API** is introduced for the redesign.
- **Routes to implement:** `/` (Overview), `/projects`, `/project/{project_id}?tab=current|sessions|handoffs|launches`, `/schedule?view=upcoming|history&page=N`, `/diagnostics`, `/settings`. Unknown tab/view → destination default. Invalid project id → existing 404.
- **No dead ends.** A destination appears in the shared shell nav **only when its route is functional and tested**. Nav grows per milestone.
- **Settings** separates browser-local preferences (Appearance, Density, Safe launch defaults — `localStorage`) from **read-only** effective machine configuration. Bridge never rewrites `config.toml` or Claude settings in V1.
- **Permission mode** always begins at **Ask as usual** and is **never persisted or pre-armed** — not by a handoff, not by a stored browser default. `bypassPermissions` keeps conspicuous wording + risk treatment.
- **No full-page reload** after pin, hide, restore, refresh, save, launch, or schedule actions.
- **Never `innerHTML` from server data.** Live updates patch `textContent` + safe attributes, toggle `hidden` on already-rendered shells, and move existing card nodes by server order via `append`. Never replace a project workspace, prompt field, or handoff textarea.
- **Preserve textarea DOM identity and user-entered values** across all live updates. `data-*` hooks stay stable; if any is renamed, its JS *and* its node-test-harness selector *and* any mutation-spec anchor migrate in the same commit.
- **Shared view models + Jinja macros**, not duplicated project/schedule markup. The workspace helper reuses the same project/card sources as Overview and Projects and must not re-probe git or live state through a divergent path.
- **Honest history limits.** The three workspace history tabs keep "up to 50 most recent rows, newest first," stated explicitly. Server-side pagination only on the Schedule **History** view (the only Store read with native `limit`/`offset`: `scheduled_runs(status, limit, offset)` + `count_scheduled_runs(status)`).
- **Three-layer tokens** (primitive → semantic → component). Components reference tokens, never raw color. **All color literals are six-digit lowercase hex** so `tests/test_contrast.py`'s parser (`(--[a-z-]+):\s*(#[0-9a-fA-F]{6})`) continues to test the real palette. Every fg/bg pair and every visible control boundary passes the existing contrast gates before use.
- **Fonts bundled locally** — Atkinson Hyperlegible Next (UI/prose) + IBM Plex Mono (paths/branches/IDs/times/tokens/prompt previews), `font-display: swap`, with `OFL.txt` licenses and a provenance note. **No external font CDN.** System fallbacks keep content usable if a font asset fails.
- **Accessibility (WCAG 2.2 AA):** one logical `h1` per page, sequential headings, skip link as first focusable control, landmarks, source order = visual order, visible 2px focus ring with offset in every theme, native controls, visible labels, state never by color alone, `aria-current` on selected nav/tab, `aria-expanded`+`aria-controls` on disclosures, meaningful live-region announcements only for state transitions, reduced-motion respected, focus moves to the new page heading on route/tab change when browser navigation doesn't already reset it.
- **Responsive:** content-driven at 320/375/768/1024/1440/2560 CSS px + 200% zoom. Large = 208px labeled sidebar + bounded canvas (`main` max readable width, extra width → whitespace/secondary column). Medium/Narrow = labeled **Menu** disclosure. No horizontal scroll to read prose or reach controls; tables use a labeled horizontal-scroll region only when they genuinely cannot reflow.
- **Motion:** color/border/shadow transitions 150–200ms; never animate layout dimensions or delay input; reduced-motion removes translate/scale, keeps opacity/color. Async feedback within 100ms; buttons keep their label + add a progress state.
- **Do not** alter scheduler semantics, storage guarantees, `~/.bridge` live data, the installed LaunchAgent, or unrelated backend behavior. Use a **separate dev port** (`BRIDGE_PORT=8788`) and temp config/data dirs for browser QA. Never `git reset --hard`, `git clean`, or destructive checkout. Never stage/commit `.superpowers/`, `.agents/`, `.DS_Store`, `tools/.DS_Store` (use explicit `git add <paths>`, never `git add -A`).

---

## File Structure

**New Python read-model modules** (thin view assembly over the existing Store; no new SQL):
- `src/bridge/overview.py` — `OverviewModel` + `build_overview(store, cfg, ...)`: attention items, recent projects, upcoming schedule, totals, diagnostics alert, freshness.
- `src/bridge/projects_view.py` — `ProjectsModel` + `build_projects(store, cfg, ...)`: complete project summaries + filter counts.
- `src/bridge/workspace.py` — `WorkspaceModel` + `build_workspace(store, cfg, project_id, tab, ...)`: current project summary, live state, queued handoff, launch options, git state, usage, selected history tab. Reuses `cards.build_cards`/one card.
- `src/bridge/schedule_view.py` — `ScheduleModel` + `build_schedule(store, view, page, ...)`: attention runs, upcoming runs, paged history, totals.
- `src/bridge/settings_view.py` — `SettingsModel` + `build_settings(cfg, hook_state)`: browser-preference defaults, effective read-only config, hook status.
- Overview/Projects/Workspace share one `ProjectSummary` projection helper so the three cannot drift; it wraps a `cards.Card` (the same source), never a second git/live probe.

**New/changed templates:**
- `templates/base.html` — rewritten shared shell (skip link, sidebar, header slot, `<main>`).
- `templates/_shell.html` — nav + freshness/connection macros (`app_nav`, `freshness_status`, `menu_disclosure`).
- `templates/_components.html` — shared macros: `project_summary_row`, `status_pill`, `empty_state`, `history_table_shell`, `launch_options` (wraps existing launch band markup), `schedule_row`.
- `templates/overview.html` (new `/`), `templates/projects.html` (new), `templates/schedule.html` (new), `templates/settings.html` (new).
- `templates/project.html` — recomposed into tabbed workspace; `templates/_workspace_current.html`, `_workspace_history.html` partials.
- `templates/dashboard.html` — deleted once `/` renders `overview.html` (its card/compose/launch/handoff/schedule markup migrates into the workspace partials + macros).
- `templates/diagnostics.html` — recomposed into grouped sections.

**Static:**
- `static/app.css` — three-layer tokens + shell + components (rewrite; keeps six-digit lowercase hex primitives).
- `static/fonts/` — `atkinson-hyperlegible-next-{400,700}.woff2`, `ibm-plex-mono-{400,600}.woff2`, `OFL-Atkinson.txt`, `OFL-IBMPlexMono.txt`, `PROVENANCE.md`.
- `static/shell.js` — Menu disclosure toggle + focus-to-heading on load (progressive; degrades to always-open nav with no JS).
- `static/settings.js` — browser-local Appearance/Density/safe-launch-default persistence + theme/density application; never persists permission.
- `static/live.js`, `static/projects.js`, `static/schedule.js`, `static/launch.js`, `static/copy.js` — **hook names preserved**; live.js gains an Overview-aware branch (patches freshness + running counts + `[data-live-path]` state words; skips git/burn/sparkline leaves absent on Overview).

**Tests:** extend `tests/test_contrast.py`; add `tests/test_overview.py`, `tests/test_projects_route.py`, `tests/test_workspace.py`, `tests/test_schedule_route.py`, `tests/test_settings.py`, `tests/test_shell.py`; extend `tests/test_static_js.py` for `shell.js`/`settings.js` and the Overview live branch. New mutation specs under `tools/mutations/` per new route.

---

## Milestone 1 — Shared shell, tokens, typography, component states

*Slice 1 of the spec's migration boundaries. After this milestone Bridge still works end-to-end; nav lists only the two functional routes (Overview `/`, Diagnostics `/diagnostics`). The existing `/` and `/project/{id}` render inside the new shell unchanged in behavior.*

### Task 1.1: Three-layer token system in `app.css` + contrast gates

**Files:**
- Modify: `src/bridge/static/app.css` (token blocks at top)
- Test: `tests/test_contrast.py`

**Interfaces:**
- Produces primitive tokens (hex, six-digit lowercase) consumed by every later component: light `--p-nav #152125`, `--p-canvas #f4f6f5`, `--p-surface #ffffff`, `--p-text #182427`, `--p-text-2 #657378`, `--p-rule #d9e1df`, `--p-work #176579`, `--p-work-soft #dcecef`, `--p-risk #a64f2f`, `--p-risk-soft #f6e6df`, plus `--p-field-line #6b767c` (control boundary ≥3:1). Dark: `--p-nav #0f191c`, `--p-canvas #12191b`, `--p-surface #192326`, `--p-raised #202d30`, `--p-text #eef3f2`, `--p-text-2 #a9b7b8`, `--p-rule #38474a`, `--p-work #77b4c0`, `--p-work-soft #213e44`, `--p-risk #e19a7e`, `--p-risk-soft #402820`, `--p-field-line #6f7c80`.
- Produces semantic aliases: `--canvas`, `--surface`, `--surface-raised`, `--text`, `--text-secondary`, `--rule`, `--work`, `--work-soft`, `--risk`, `--risk-soft`, `--field-line`, `--focus` (=`--p-work`), `--nav`.

- [ ] **Step 1: Extend the failing contrast test.** In `tests/test_contrast.py`, replace the `PAIRS` list so it references the new primitive token names and every new semantic pair. Add pairs (fg, bg, min, label):
  ```python
  PAIRS = [
      ("--p-text", "--p-canvas", 4.5, "body text on canvas"),
      ("--p-text", "--p-surface", 4.5, "body text on a surface"),
      ("--p-text-2", "--p-canvas", 4.5, "secondary text on canvas"),
      ("--p-text-2", "--p-surface", 4.5, "secondary text on a surface"),
      ("--p-work", "--p-surface", 4.5, "work-accent text/link on a surface"),
      ("--p-work", "--p-canvas", 4.5, "work-accent text/link on canvas"),
      ("--p-risk", "--p-surface", 4.5, "risk text on a surface"),
      ("--p-risk", "--p-risk-soft", 4.5, "risk text on risk-soft pill"),
      ("--p-work", "--p-work-soft", 4.5, "work text on work-soft pill"),
      # Non-text UI boundaries need 3:1 (WCAG 1.4.11).
      ("--p-work", "--p-surface", 3.0, "focus ring against a surface"),
      ("--p-field-line", "--p-surface", 3.0, "form control border on a surface"),
      ("--p-field-line", "--p-canvas", 3.0, "form control border on canvas"),
      ("--p-rule", "--p-surface", 1.0, "hairline rule (decorative, no min)"),
  ]
  ```
  Keep `test_the_stylesheet_defines_both_themes` but assert on `--p-text-2` (`t["light"]["--p-text-2"] != t["dark"]["--p-text-2"]`). Keep `test_no_pure_black_or_white_surfaces` but read `--p-canvas`/`--p-text`.

- [ ] **Step 2: Run to verify it fails.** `uv run pytest tests/test_contrast.py -q` → FAIL (`--p-text is not defined`).

- [ ] **Step 3: Write the token blocks.** At the top of `app.css`, define `:root` primitive + semantic + timing/space/type/radius layers, and a `@media (prefers-color-scheme: dark)` override plus explicit `[data-theme="light"]` / `[data-theme="dark"]` attribute overrides on `:root` (so Settings can force a theme). Primitives use the exact hex above. Semantic layer maps `--canvas: var(--p-canvas)` etc. Add spacing (`--space-1..-10` on a 4px base: `.25/.5/.75/1/1.5/2/2.5rem`…), type scale (`--text-xs .75rem` … `--text-body 1rem` … `--text-h1 1.5rem`), radii (`--radius-6 6px`…`--radius-12 12px`), timing (`--motion-fast 150ms`, `--motion 200ms`). Keep `color-scheme: light dark`.

- [ ] **Step 4: Run to verify it passes.** `uv run pytest tests/test_contrast.py -q` → PASS (both themes, every pair). If a pair fails, adjust only the failing primitive by the minimum needed and re-run; record the measured ratio in a CSS comment next to the token (matching the existing `--field-line` comment style).

- [ ] **Step 5: Commit.**
  ```bash
  git add src/bridge/static/app.css tests/test_contrast.py
  git commit -m "Add three-layer token system with contrast gates for redesign palette"
  ```

### Task 1.2: Bundle Atkinson Hyperlegible Next + IBM Plex Mono locally

**Files:**
- Create: `src/bridge/static/fonts/*.woff2`, `OFL-Atkinson.txt`, `OFL-IBMPlexMono.txt`, `PROVENANCE.md`
- Modify: `src/bridge/static/app.css` (`@font-face` + `--font-sans`/`--font-mono` tokens)
- Test: `tests/test_fonts.py` (new)

**Interfaces:**
- Produces `--font-sans: "Atkinson Hyperlegible Next", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;` and `--font-mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace;`.

- [ ] **Step 1: Fetch fonts with verified provenance.** Download woff2 + OFL licenses from official upstreams (Atkinson Hyperlegible Next: Google Fonts `github.com/googlefonts/atkinson-hyperlegible` / fonts.google.com; IBM Plex Mono: `github.com/IBM/plex` releases). Save weights 400 + 700 (sans) and 400 + 600 (mono) to `static/fonts/`. Record source URL, release/tag, and sha256 of each file in `PROVENANCE.md`. If network egress is blocked in the execution sandbox, STOP and surface this as a required-external-resource decision (per the workflow's stop conditions) — do not fall back to a CDN.

- [ ] **Step 2: Write the failing test.**
  ```python
  # tests/test_fonts.py
  from pathlib import Path
  STATIC = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static"
  CSS = (STATIC / "app.css").read_text()

  def test_fonts_are_self_hosted_not_cdn():
      assert "fonts.googleapis.com" not in CSS and "fonts.gstatic.com" not in CSS
      assert "@font-face" in CSS
      assert 'src: url("/static/fonts/' in CSS

  def test_font_files_and_licenses_present():
      fonts = STATIC / "fonts"
      assert (fonts / "OFL-Atkinson.txt").exists()
      assert (fonts / "OFL-IBMPlexMono.txt").exists()
      assert list(fonts.glob("atkinson-hyperlegible-next-*.woff2"))
      assert list(fonts.glob("ibm-plex-mono-*.woff2"))

  def test_font_face_uses_swap():
      assert CSS.count("font-display: swap") >= 2
  ```

- [ ] **Step 3: Run to verify it fails.** `uv run pytest tests/test_fonts.py -q`.

- [ ] **Step 4: Add `@font-face` + tokens.** Four `@font-face` blocks with `font-display: swap`, `src: url("/static/fonts/<file>.woff2") format("woff2")`, correct `font-weight`. Set `--font-sans`/`--font-mono` and switch `body { font-family: var(--font-sans); }` and every existing `var(--mono)`/`var(--display)` reference to the new tokens (retain the old `--mono`/`--sans`/`--display` names as aliases pointing at the new tokens to avoid churn in this task; they get cleaned up in Task 1.4).

- [ ] **Step 5: Run to verify it passes.** `uv run pytest tests/test_fonts.py tests/test_contrast.py -q`.

- [ ] **Step 6: Commit.**
  ```bash
  git add src/bridge/static/app.css src/bridge/static/fonts tests/test_fonts.py
  git commit -m "Bundle Atkinson Hyperlegible Next and IBM Plex Mono locally with licenses"
  ```

### Task 1.3: Shared app shell + sidebar navigation

**Files:**
- Modify: `src/bridge/static/app.css` (shell/nav/header layout)
- Create: `src/bridge/templates/_shell.html` (macros `app_nav`, `freshness_status`)
- Modify: `src/bridge/templates/base.html`
- Create: `src/bridge/static/shell.js`
- Modify: `src/bridge/templates/dashboard.html`, `project.html`, `diagnostics.html` (adopt shell blocks: `{% block page_title %}`, `{% block page_actions %}`, `{% block content %}`)
- Test: `tests/test_shell.py` (new); extend `tests/test_static_js.py`

**Interfaces:**
- `app_nav(active)` renders the sidebar `<nav aria-label="Primary">` with groups **Workspace** (Overview→`/`) and **System** (Diagnostics→`/diagnostics`). The active item carries `aria-current="page"`. Each item pairs an outlined SVG icon with a visible label. **Projects/Schedule/Settings entries are added by their milestones** (Task 2.5, 4.2, 5.2) — this task ships only the two live routes so nav has no dead end.
- `freshness_status(state, age, action=None)` renders the connection/freshness region with a state word + age; never color alone.
- Base template exposes named blocks: `{% block page_title %}`, `{% block page_summary %}`, `{% block page_actions %}`, `{% block content %}`, and keeps loading the existing scripts (order preserved: copy → launch → schedule → live → projects), adding `shell.js` first and `settings.js` last.

- [ ] **Step 1: Write failing shell tests.**
  ```python
  # tests/test_shell.py
  from fastapi.testclient import TestClient
  from bridge.api import create_app
  from bridge.config import load
  from bridge.store import Store

  def _client(tmp_path):
      cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool"})
      return TestClient(create_app(Store(cfg.db_path), cfg))

  def test_skip_link_is_first_focusable(tmp_path):
      html = _client(tmp_path).get("/").text
      assert html.index('href="#main"') < html.index('<nav')
      assert 'id="main"' in html

  def test_one_h1_and_landmarks(tmp_path):
      import re
      html = _client(tmp_path).get("/").text
      assert len(re.findall(r"<h1\b", html)) == 1
      assert '<nav aria-label="Primary"' in html
      assert '<main id="main"' in html

  def test_active_nav_marks_aria_current(tmp_path):
      html = _client(tmp_path).get("/").text
      assert re.search(r'href="/"[^>]*aria-current="page"', html) or \
             re.search(r'aria-current="page"[^>]*href="/"', html)

  def test_nav_has_no_dead_ends_in_milestone_one(tmp_path):
      html = _client(tmp_path).get("/").text
      # Only functional routes appear.
      assert 'href="/projects"' not in html
      assert 'href="/schedule"' not in html
      assert 'href="/settings"' not in html
      assert 'href="/diagnostics"' in html
  ```
  Add a node-harness test in `tests/test_static_js.py` for `shell.js`: the Menu button toggles `aria-expanded` and the nav's `hidden` state, and with no JS the nav is not `hidden`.

- [ ] **Step 2: Run to verify failure.** `uv run pytest tests/test_shell.py -q`.

- [ ] **Step 3: Build the shell.** Rewrite `base.html`: `<a class="skip-link" href="#main">Skip to content</a>` as first `<body>` child; a `<div class="shell">` grid with `<aside class="sidebar">` (wordmark + bridge-span mark SVG, `{{ app_nav(active) }}`, connection/freshness in the side foot) and `<div class="shell__body">` containing `<header class="page-head">` (`page_title`/`page_summary`/`page_actions` blocks) and `<main id="main" tabindex="-1">{% block content %}{% endblock %}</main>`. For medium/narrow, a `<button class="menu-toggle" aria-expanded="false" aria-controls="primary-nav">Menu</button>` in a compact header; `_shell.html` gives the nav `id="primary-nav"`. Move the diagnostics-alert link into the header actions region. Update `dashboard.html`/`project.html`/`diagnostics.html` to fill the new blocks (behavior identical; the dashboard's totals/freshness/Refresh move into `page_actions`/`page_summary`). Write `shell.js` (delegated click on `.menu-toggle`; on load, if `location` has no hash, do nothing — browser handles focus; expose nothing global).

- [ ] **Step 4: Style the shell in `app.css`.** 208px sidebar grid at ≥1024px (`grid-template-columns: 208px minmax(0,1fr)`); `main { width: min(100%, 68rem); margin-inline: auto; }` bounded readable canvas; nav item 40px min height (44px at narrow); focus-visible 2px ring with offset using `--focus`; Menu disclosure shown only below 1024px; skip-link visually-hidden until focused. Reduced-motion block retained.

- [ ] **Step 5: Run tests.** `uv run pytest tests/test_shell.py tests/test_api.py tests/test_static_js.py -q` → PASS. Fix any existing `test_api.py` assertions that keyed on the old topbar structure by updating them to the new hook locations in the *same commit* (the hooks — `data-dashboard-total`, `data-freshness-strip`, `data-dashboard-refresh` — keep their names; only their container moves).

- [ ] **Step 6: Commit.**
  ```bash
  git add src/bridge/templates/base.html src/bridge/templates/_shell.html \
    src/bridge/templates/dashboard.html src/bridge/templates/project.html \
    src/bridge/templates/diagnostics.html src/bridge/static/app.css \
    src/bridge/static/shell.js tests/test_shell.py tests/test_static_js.py
  git commit -m "Introduce shared app shell with sidebar navigation"
  ```

### Task 1.4: Component tokens + interaction states

**Files:**
- Modify: `src/bridge/static/app.css`
- Test: `tests/test_contrast.py` (component-boundary pairs already covered), `tests/test_shell.py`

- [ ] **Step 1: Add a failing assertion** in `tests/test_shell.py` that the stylesheet defines the eight interaction states for buttons and a visible focus ring:
  ```python
  def test_stylesheet_defines_interaction_states():
      css = (STATIC / "app.css").read_text()  # import STATIC as in test_fonts
      for sel in (".btn:hover", ".btn:active", ".btn:focus-visible", ".btn:disabled",
                  ".btn[aria-busy=\"true\"]", ":focus-visible"):
          assert sel in css
      assert "prefers-reduced-motion" in css
  ```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Define component tokens + states.** Component layer: `--btn-bg/-fg/-border`, `--btn-bg-hover`, `--btn-primary-bg/-fg`, `--field-bg/-border`, `--pill-*`, `--nav-item-*`, `--row-*`, `--table-header-bg`, `--handoff-*` — all `var(--semantic)`. Define default/hover/pressed/focus-visible/disabled/`[aria-busy="true"]` (loading) states for `.btn`, `.btn--primary`; add `.btn--primary` (work-blue fill), keep `.btn--icon`, `.btn--pin`. Fields (`select`, `textarea`, `input`) get `--field-line` borders. Status pills (`.pill`, `.pill--work`, `.pill--risk`). Transitions limited to `color, background-color, border-color, box-shadow` at `var(--motion)`; **no** transition on width/height/inset. Reduced-motion: keep opacity/color, drop translate/scale. Remove the temporary `--mono`/`--sans`/`--display` aliases from Task 1.2, switching remaining references to `--font-mono`/`--font-sans`.

- [ ] **Step 4: Run.** `uv run pytest tests/test_shell.py tests/test_contrast.py tests/test_api.py -q` → PASS.

- [ ] **Step 5: Commit.**
  ```bash
  git add src/bridge/static/app.css tests/test_shell.py
  git commit -m "Define component tokens and full interaction-state set"
  ```

### Milestone 1 checkpoint
- [ ] `uv run pytest -q` fully green (≥760). Inspect `git diff --stat main`. Manual browser smoke at `BRIDGE_PORT=8788` on `/`, `/project/<id>`, `/diagnostics` in light + dark. No nav dead ends.

---

## Milestone 2 — Calm Overview + Projects index

*Slice 2. `/` becomes the calm attention page (no prompt textareas, launch selectors, catalog, or completed history). `/projects` is added with its nav entry. The full compose/launch/handoff surface is **not yet moved** — it currently lives on the dashboard cards; this milestone removes it from `/` and the Projects index, and Milestone 3 rehomes it in the workspace. To keep Bridge usable in between, the workspace `/project/{id}` retains the existing card-style interaction until Milestone 3 recomposes it.*

### Task 2.1: Overview read model

**Files:** Create `src/bridge/overview.py`; Test `tests/test_overview.py`

**Interfaces:**
- Produces `build_overview(store, cfg, *, live_state=None, cards=None, now=None) -> OverviewModel`. `OverviewModel` (frozen dataclass) fields: `attention: list[AttentionItem]`, `recent: list[ProjectSummary]`, `up_next: list[ScheduleRow]`, `totals: dict`, `diagnostics_alert: bool`, `freshness: dict`. `AttentionItem` fields: `kind` (`"handoff"|"running"|"schedule_failure"|"stale"`), `project_id|None`, `title`, `summary`, `primary_action` (label + href), `meta`. Reuses `cards.build_cards` (same source as Projects/Workspace) and `DashboardBuilder`'s freshness/totals; **no new probe**.

- [ ] **Step 1: Failing test** — seed a store with a queued handoff on one project, a stale dirty project, and a `failed` scheduled run; build the model with injected `agents_fn`/`probe_fn` (mirroring `test_dashboard.py`); assert attention ordering is pinned→queued→running→dirty/stale→recent→idle (the server priority contract), that a queued-handoff item's `primary_action.label == "Continue in Terminal"`, a running item's is `"Open project"`, a schedule failure's is `"Review scheduled run"`, a stale item's is `"Review project state"`, and that `recent`/`up_next` are truncated small lists.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement `overview.py`** deriving attention items from `build_cards(...)` (rank via `cards.sort_key`), recent from the same cards, `up_next` from `store.scheduled_runs(status="pending")[:N]`, schedule failures from `scheduled_runs` terminal non-cancelled with retryable status, totals/freshness/diagnostics_alert from `DashboardBuilder._envelope` fields (reuse, don't re-derive). Absolute token values only.

- [ ] **Step 4: Run → pass.** `uv run pytest tests/test_overview.py -q`.

- [ ] **Step 5: Commit.** `git add src/bridge/overview.py tests/test_overview.py && git commit -m "Add Overview read model reusing card and schedule sources"`

### Task 2.2: Shared component macros

**Files:** Create `src/bridge/templates/_components.html`; Test `tests/test_components.py` (render macros via a tiny Jinja env or through the routes that use them in later tasks — here, assert the macros exist and render expected hooks by importing `Jinja2Templates` and calling `env.get_template("_components.html").module`).

- [ ] **Step 1: Failing test** rendering `project_summary_row(summary)` and asserting it emits a single `Open project` link to `/project/{id}`, project name + `<code>` path, a `status_pill` with a state word (not color alone), branch/dirty summary, and last-session title + age; and `empty_state(msg)` emits `class="empty"` with the message; and `status_pill(state)` emits the state word as text.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement macros** `project_summary_row`, `status_pill`, `empty_state`, `history_table_shell`, `schedule_row` (the schedule_row macro is authored here and reused by both Overview `up_next`, later the Schedule page — the shared row/macro contract the spec requires). Numbers right-aligned + tabular; prose left-aligned.

- [ ] **Step 4: Run → pass. Step 5: Commit.** `git commit -m "Add shared Jinja component macros for rows, pills, and tables"`

### Task 2.3: Render calm Overview at `/`

**Files:** Create `templates/overview.html`; Modify `src/bridge/api.py` (`dashboard` route → render overview via `build_overview`); delete card/compose markup from the `/` path (dashboard.html retired for `/`); Test `tests/test_overview.py` (route section), extend `tests/test_api.py`.

- [ ] **Step 1: Failing route tests** — `GET /` (a) contains no `<textarea` (`assert html.count("<textarea") == 0`), (b) contains no `data-launch-model`/`data-compose-prompt`, (c) shows the attention section heading, a `Continue in Terminal` action for a queued handoff, a `View all projects` link to `/projects`, and the freshness region with a state word + `data-dashboard-refresh` Refresh button, (d) keeps `data-freshness-strip` and `data-dashboard-total=` hooks for live patching.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement.** Point the `/` route at `build_overview` + `overview.html` (page heading, plain-language attention summary, freshness+Refresh; Needs attention; Recent projects via `project_summary_row`; Up next via `schedule_row`; quiet usage summary, absolute values). Retain `data-freshness-strip`/`data-dashboard-total`/`data-dashboard-refresh`/`data-project-membership-status` hooks so live.js keeps working. Keep the `/project/{id}` route unchanged for now (still renders the interactive card surface, reached via `Open project`).

- [ ] **Step 4: Run.** `uv run pytest tests/test_overview.py tests/test_api.py -q`. Update/relocate any `test_api.py` dashboard assertions that expected cards on `/` — move those expectations to the workspace tests in Milestone 3 or delete assertions for behavior that legitimately left `/` (never weaken a still-valid contract; only retarget assertions whose subject moved).

- [ ] **Step 5: Author mutation spec** `tools/mutations/overview-attention.json` (bare-list form): anchor the attention-ordering call and a primary-action label, each naming a `tests/test_overview.py::...` node. Verify anchors: `uv run pytest tests/test_mutation_specs.py -q`. Falsify: `uv run python tools/falsify.py --spec tools/mutations/overview-attention.json` (commit tree first). Both mutations must be `CAUGHT`.

- [ ] **Step 6: Commit.**
  ```bash
  git add src/bridge/api.py src/bridge/templates/overview.html \
    tests/test_overview.py tests/test_api.py tools/mutations/overview-attention.json
  git rm src/bridge/templates/dashboard.html   # only if no route references it
  git commit -m "Render calm Overview at / and retire the mega-dashboard"
  ```

### Task 2.4: Live updates on Overview

**Files:** Modify `src/bridge/static/live.js`; extend `tests/test_static_js.py`; extend SSE payload only if needed (prefer reuse of the existing envelope).

**Interfaces:** Consumes the existing `/events` `snapshot`/`update`/`patch` envelope. Overview has no per-card git/burn/sparkline leaves, so `applyDashboardUpdate` must **skip missing leaves without error** (guard every `card.querySelector` result). It still patches: topbar totals (`[data-dashboard-total]`), freshness strip, diagnostics alert, and `[data-live-path]` state words present in attention/recent rows.

- [ ] **Step 1: Failing node-harness test** in `tests/test_static_js.py`: feed a snapshot to `applyDashboardUpdate` against an Overview-shaped DOM stub (freshness strip + totals + one `[data-live-path]` row, no `[data-project-card]` git/burn leaves) and assert no throw, totals updated, freshness state announced, and the live word patched. Reassert the existing dashboard-shaped harness still passes (identity/values invariants unchanged).

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement guards** in `live.js`: every leaf write checks the node exists first; card iteration tolerates absent cards; reorder path only runs when `[data-cards-list]` exists. No hook renamed. No `innerHTML`. Textarea rule intact (Overview has none).

- [ ] **Step 4: Run.** `uv run pytest tests/test_static_js.py -q` → PASS (all pre-existing live/freshness harness assertions included).

- [ ] **Step 5: Commit.** `git commit -m "Make live updates tolerate the leaf-light Overview DOM"`

### Task 2.5: Projects index route

**Files:** Create `src/bridge/projects_view.py`, `templates/projects.html`; Modify `api.py` (`GET /projects`), `_shell.html` (add Projects nav entry), `static/projects.js` (client-side search/filter), `app.css`; Tests `tests/test_projects_route.py`; extend `tests/test_static_js.py`; mutation spec `tools/mutations/projects-index.json`.

**Interfaces:** `build_projects(store, cfg, *, live_state=None, cards=None) -> ProjectsModel` with `rows: list[ProjectSummary]` (existing actionability order via `cards.sort_key`), `counts: dict` (all/needs_attention/running/queued/hidden), and `hidden: list[...]`. No new query.

- [ ] **Step 1: Failing tests** — `GET /projects` returns 200 with: a labeled `Search projects` input, filter controls for All/Needs attention/Running/Queued/Hidden with a live result count, one `Open project` row action per project (no `<textarea>`, no `data-launch-model`), branch/dirty summary + last-session age, and Pin/Hide/Restore as secondary actions in a labeled menu (reuse existing `data-project-pin`/`data-project-hide`/`data-project-restore` hooks). Node-harness test: `projects.js` filters rows locally by name/path against the rendered projection and updates the count + exposes an explicit empty state, without a fetch.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** the model, template (using `project_summary_row`), the nav entry (now Projects is functional → add it, `aria-current` when active), and the `projects.js` search/filter enhancement (progressive: server renders the full stable list; JS filters the DOM, announces count into a `role=status`, never reloads). Pin/Hide/Restore keep the existing PATCH-and-patch-DOM behavior (no reload).

- [ ] **Step 4: Run.** `uv run pytest tests/test_projects_route.py tests/test_static_js.py tests/test_api.py -q`.

- [ ] **Step 5: Mutation spec** `projects-index.json` (search-field label + filter-count + Open-project action anchors). Verify + falsify → CAUGHT.

- [ ] **Step 6: Commit.**
  ```bash
  git add src/bridge/projects_view.py src/bridge/templates/projects.html \
    src/bridge/templates/_shell.html src/bridge/api.py src/bridge/static/projects.js \
    src/bridge/static/app.css tests/test_projects_route.py tests/test_static_js.py \
    tools/mutations/projects-index.json
  git commit -m "Add searchable Projects index route and nav entry"
  ```

### Milestone 2 checkpoint
- [ ] Full suite green. Browser QA `/` and `/projects` in light+dark at 1440/375. Verify no dead nav; Overview has no textareas/launch selectors; Projects search/filter works keyboard-only; pin/hide/restore don't reload.

---

## Milestone 3 — Project workspace + server-rendered tabs

*Slice 3. Rehomes the compose/launch/handoff/schedule surface into `/project/{id}?tab=current`, using the existing `data-*` hooks and JS verbatim so `test_static_js.py` stays green. History moves to Sessions/Handoffs/Launches tabs.*

### Task 3.1: Workspace read model

**Files:** Create `src/bridge/workspace.py`; Test `tests/test_workspace.py`

**Interfaces:** `build_workspace(store, cfg, project_id, tab, *, live_state=None) -> WorkspaceModel | None` (None → 404). Fields: `project` (row), `card` (the single `cards.Card` for this project — same source as Overview/Projects, reused not re-probed), `handoff` (queued or None), `git` (from `get_git_cache`, as the current detail route does), `sessions`/`handoffs`/`launches` (only the selected `tab`'s list, default `current`), `tab` (normalized; unknown → `"current"`), `session_metas`. Launch options come off `card.launch_models/efforts/permission_modes`.

- [ ] **Step 1: Failing tests** — build for a project with a queued handoff → `handoff` present, `card` identity equals a `build_cards` card for that id, unknown tab normalizes to `current`, invalid id → None. Assert git comes from `get_git_cache` (mutation-guarded later) not a fresh probe.

- [ ] **Step 2–4:** Run→fail; implement reusing `build_cards` (filter to the one project) + `get_git_cache` + the existing `sessions/handoffs/launches` 50-capped reads + `sessionmeta.read_many`; run→pass.

- [ ] **Step 5: Commit.** `git commit -m "Add Project workspace read model reusing card sources"`

### Task 3.2: Tabbed workspace route + header

**Files:** Modify `api.py` (`detail` route → `?tab=`), `templates/project.html` (shell blocks, breadcrumb, Pin/Hide, tab nav); create `_workspace_current.html`, `_workspace_history.html`; Tests `tests/test_workspace.py`.

- [ ] **Step 1: Failing tests** — `GET /project/{id}` defaults to `tab=current`; `?tab=sessions|handoffs|launches` selects that tab with `aria-current="page"` on the tab link and the URL reflecting it; unknown `?tab=zzz` → current view (200, not blank); invalid id → 404 (unchanged). Breadcrumb `Projects / {name}` links to `/projects`. Pin/Hide are explicit project actions using existing hooks. One `<h1>` (project name).

- [ ] **Step 2–3:** Run→fail; implement the route (`tab: str = "current"`, normalized in the model), header with breadcrumb + full path + Pin/Hide + tab `<nav aria-label="Project sections">` where each tab is a real `<a href="?tab=...">` (full page load, predictable Back), `aria-current="page"` on the active one, counts from the 50-capped reads.

- [ ] **Step 4–5:** Run→pass; commit `git commit -m "Add server-rendered project workspace tabs"`.

### Task 3.3: Current work tab (continuation-first)

**Files:** Create/fill `_workspace_current.html`; Modify `app.css`; reuse `launch.js`/`schedule.js`/`copy.js` unchanged; Tests `tests/test_workspace.py`; mutation spec `tools/mutations/workspace-current.json`.

- [ ] **Step 1: Failing tests** — with a queued handoff: the Current tab shows handoff summary + age, the session→handoff→next span line **only when those states exist**, a concise saved-prompt preview, an `Edit prompt` control, exactly one primary `Continue in Terminal` button, a secondary `Schedule` action, and a `Change options` disclosure (`aria-expanded`/`aria-controls`) wrapping model/effort/permission where permission's selected option is the no-flag `Ask as usual` (assert the perm select's first/selected option value is empty and is never driven by a suggestion). A separate `Start a different session` section reveals one labeled prompt field + the same launch options and does **not** clear the handoff. The lifecycle action is named `Dismiss handoff`. With no handoff: a short empty state + `Start session` primary. Preserve textarea identity: `assert html.count("<textarea") == html.count("</textarea>")` and the handoff textarea keeps `data-prompt-handoff` and the launch band keeps `data-launch`/`data-launch-model`/`data-launch-perm`/`data-launch-button` with ids keyed off `project_id`.

- [ ] **Step 2–3:** Run→fail; implement `_workspace_current.html` by lifting the existing `_card.html` compose/handoff/launch/schedule markup into the workspace layout, preserving every `data-*` hook and id scheme, adding the span-line (rendered only when `card.session` ended AND `handoff` queued), the saved-prompt preview + `Edit prompt` toggling the existing textarea, the `Change options` disclosure around the launch band, and the secondary Project state panel (live session state, branch, dirty/ahead/behind, remote, token totals, freshness) that never visually competes with the primary action. `Continue in Terminal` reuses the launch band's `data-launch-button` path; `Dismiss handoff` PATCHes `status=dismissed` via the existing handoff hook.

- [ ] **Step 4:** Run `uv run pytest tests/test_workspace.py tests/test_static_js.py -q` — the JS harness must stay green because hooks are unchanged.

- [ ] **Step 5:** Mutation spec `workspace-current.json`: anchor (a) the permission-never-armed default, (b) the single-primary-button contract, (c) the span-line-only-when-states-exist guard. Verify + falsify → CAUGHT.

- [ ] **Step 6: Commit.** `git commit -m "Rehome continuation-first handoff surface into the workspace Current tab"`

### Task 3.4: Sessions / Handoffs / Launches tabs

**Files:** Create/fill `_workspace_history.html`; Modify `app.css`; Tests `tests/test_workspace.py`.

- [ ] **Step 1: Failing tests** — each history tab renders the existing table (sessions/handoffs/launches) inside `history_table_shell` (sticky opaque header, labeled `role="region"` scroll only when needed, status words + color, tabular numerals, visible empty state), each says "Showing up to 50 most recent records" explicitly, and no sortable affordance is present. Reuse the exact table markup from today's `project.html`.

- [ ] **Step 2–5:** Run→fail; implement using the migrated tables + `history_table_shell`; run→pass; commit `git commit -m "Add workspace Sessions, Handoffs, and Launches history tabs"`.

### Milestone 3 checkpoint
- [ ] Full suite green. Browser QA the workspace: continue-in-terminal path, edit prompt (verify the textarea keeps typed text through a live tick), change-options discloses, start-a-different-session doesn't clear the handoff, dangerous permission selection is conspicuous, tabs change via Back/Forward.

---

## Milestone 4 — Schedule + Diagnostics recomposition

*Slice 4. Adds `/schedule` (+ nav) and regroups Diagnostics. Scheduler semantics untouched — presentation only.*

### Task 4.1: Schedule read model

**Files:** Create `src/bridge/schedule_view.py`; Test `tests/test_schedule_route.py` (model section).

**Interfaces:** `build_schedule(store, *, view="upcoming", page=0, page_size=25) -> ScheduleModel`. Upcoming: `attention` (failed/missed/indeterminate), `pending` (chronological), `launching`. History: terminal rows via `store.scheduled_runs(limit=page_size, offset=page*page_size)` filtered to terminal + `count_scheduled_runs` for total/paging. Unknown `view` → `"upcoming"`.

- [ ] **Step 1–4:** Failing tests (upcoming grouping order; history pagination totals; unknown view → upcoming); run→fail; implement (pagination confined here, using the native `limit/offset` + count); run→pass.
- [ ] **Step 5: Commit.** `git commit -m "Add Schedule read model with paged history"`

### Task 4.2: Schedule route + shared row contract

**Files:** Create `templates/schedule.html`; Modify `api.py` (`GET /schedule`), `_shell.html` (Schedule nav entry), `overview.html` (Up next uses the same `schedule_row`), `static/schedule.js` (no hook changes; ensure Edit/Cancel/Run now/Retry work on the new page), `app.css`; Tests `tests/test_schedule_route.py`; mutation spec `tools/mutations/schedule-route.json`.

- [ ] **Step 1: Failing tests** — `GET /schedule` default Upcoming groups attention→pending→launching; `?view=history&page=1` paginates terminal runs with prev/next reflecting `X-Total`-style counts; unknown `?view=zzz` → upcoming; the Overview `Up next` rows and the Schedule rows share the **same** `schedule_row` macro (assert identical state vocabulary + accessible labels, e.g. both emit `data-scheduled-state` and the same status word). Existing action hooks (`data-scheduled-run-now`/`-edit-toggle`/`-cancel`/`-retry`) present with disabled/loading/success/error affordances; focus moves to the replacing control or section heading on removal. A calendar grid is absent.

- [ ] **Step 2–3:** Run→fail; implement the route + template reusing `schedule_row`; add the functional nav entry.

- [ ] **Step 4:** Run `uv run pytest tests/test_schedule_route.py tests/test_static_js.py tests/test_api.py -q`.
- [ ] **Step 5:** Mutation spec `schedule-route.json` (shared-macro vocabulary + unknown-view-defaulting + pagination-offset anchors). Verify + falsify → CAUGHT.
- [ ] **Step 6: Commit.** `git commit -m "Add Schedule route with shared row macro and paged history"`

### Task 4.3: Diagnostics recomposition

**Files:** Modify `templates/diagnostics.html`, `api.py` `_diagnostics` grouping (presentation grouping only — no new probe), `app.css`; Tests extend `tests/test_api.py`.

- [ ] **Step 1: Failing tests** — Diagnostics groups into Needs attention (only failing/degraded checks, each with cause + next action), Runtime (liveness source, running sessions, Claude version), Indexing (last run, files seen/scanned, parsed lines, duration, errors), Storage (queued handoffs, spool depth). When nothing is wrong it says **Bridge is healthy** and keeps routine facts quiet. One `<h1>`, shared shell.

- [ ] **Step 2–5:** Run→fail; implement grouping from the existing `_diagnostics()` dict (no backend change); run→pass; commit `git commit -m "Regroup Diagnostics into attention, runtime, indexing, and storage"`.

### Milestone 4 checkpoint
- [ ] Full suite green. Browser QA `/schedule` upcoming+history, retry/cancel focus management, `/diagnostics` healthy + degraded states.

---

## Milestone 5 — Settings, hardening, final verification

*Slice 5. Adds `/settings` (+ nav), applies browser-local theme/density, and completes responsive/a11y hardening + full verification.*

### Task 5.1: Settings read model

**Files:** Create `src/bridge/settings_view.py`; Test `tests/test_settings.py`.

**Interfaces:** `build_settings(cfg, hook_state=None) -> SettingsModel`: `effective` (config-file path, project/session metadata dirs, stale threshold, aliases, archived paths, database path, port — all read-only from `Config`), `hook_status` (present/absent + setup/recovery guidance), `launch_defaults` (the catalogs for the browser-local safe-launch-default selectors). No secrets, no write path.

- [ ] **Step 1–4:** Failing tests (effective config surfaces the exact `Config` fields read-only; no mutation endpoint referenced); run→fail; implement; run→pass.
- [ ] **Step 5: Commit.** `git commit -m "Add read-only Settings model for effective configuration"`

### Task 5.2: Settings route + browser-local preferences

**Files:** Create `templates/settings.html`, `static/settings.js`; Modify `api.py` (`GET /settings`), `_shell.html` (Settings nav entry), `base.html` (early inline theme/density bootstrap to avoid FOUC), `app.css` (density tokens); Tests `tests/test_settings.py`; extend `tests/test_static_js.py`; mutation spec `tools/mutations/settings-route.json`.

- [ ] **Step 1: Failing tests** — `GET /settings`: Appearance (System/Light/Dark), Density (Comfortable/Compact) described as token-driven and never below accessible target sizes, Safe launch defaults (model/effort/terminal-or-background) labeled **stored in this browser**, a **read-only** permission statement that permissions always begin at **Ask as usual**, an Effective configuration section listing the `Config` fields as read-only text (no inputs that POST), and Hook status with setup/recovery instructions. Assert there is **no** `<form method=post>` / write control targeting machine config. Node-harness test for `settings.js`: setting Appearance=Dark writes `localStorage` and sets `data-theme="dark"` on `<html>`; changing a safe-launch default persists to `localStorage`; **permission is never written to `localStorage`** (assert no permission key appears) and the launch band always initializes perm to the empty/no-flag value regardless of any stored default.

- [ ] **Step 2–3:** Run→fail; implement the template (native controls, visible labels), `settings.js` (reads/writes only `bridge.appearance`, `bridge.density`, `bridge.launch.model|effort|mode`; applies `data-theme`/`data-density` on `<html>`; System follows `matchMedia("(prefers-color-scheme: dark)")`), and an early tiny inline bootstrap in `<head>` that applies stored theme/density before paint. Add the functional Settings nav entry. Density maps to component-token spacing overrides that never drop control heights below 44px on touch.

- [ ] **Step 4:** Run `uv run pytest tests/test_settings.py tests/test_static_js.py -q`.
- [ ] **Step 5:** Mutation spec `settings-route.json` (permission-read-only-statement + no-write-control + Ask-as-usual anchors). Verify + falsify → CAUGHT.
- [ ] **Step 6: Commit.** `git commit -m "Add bounded Settings route with browser-local preferences"`

### Task 5.3: Permission-mode-never-persisted proof

**Files:** Tests `tests/test_settings.py`, `tests/test_static_js.py` (strengthen); no product change expected unless a gap is found.

- [ ] **Step 1: Failing/So-far-absent test** — a node-harness test that, with `localStorage` seeded with a (hypothetical) permission value AND safe-launch defaults, `bridgeLaunchBody` still sends `permission_mode: ""` and the rendered launch band's perm select is the no-flag option. Prove stored browser defaults influence model/effort/mode **but never permission**.
- [ ] **Step 2–4:** Run→fail (if a leak exists, fix it minimally in `settings.js`/`launch.js`); run→pass.
- [ ] **Step 5: Commit.** `git commit -m "Prove permission mode always begins at Ask as usual"`

### Task 5.4: Responsive + accessibility hardening

**Files:** Modify `src/bridge/static/app.css`; Tests `tests/test_shell.py` (media-query presence + focus ring), manual browser QA.

- [ ] **Step 1: Failing test** asserting the stylesheet defines the breakpoints and reflow rules (Menu disclosure < 1024px, single-column < 768px, 44px targets at narrow, `main` bounded width, table horizontal-scroll region only where needed) — assert presence of the relevant `@media` queries and `min-height: 44px`/`2.75rem` at narrow.
- [ ] **Step 2–3:** Run→fail; implement the responsive layers (large sidebar / medium+narrow Menu; content-driven columns; 200%-zoom safe — no sticky chrome hiding focused controls).
- [ ] **Step 4:** Run `uv run pytest -q` full suite → green.
- [ ] **Step 5: Commit.** `git commit -m "Complete responsive and accessibility hardening"`

### Task 5.5: Final verification + browser QA

**Files:** none (verification); capture screenshots to `docs/superpowers/plans/assets/redesign/` (git-ignored path is fine; record paths in the final report).

- [ ] **Step 1: Full suite** — `uv run pytest -q`. Record the count; must be ≥ baseline 760 + new tests, zero failures.
- [ ] **Step 2: Focused suites** — `uv run pytest tests/test_contrast.py tests/test_shell.py tests/test_overview.py tests/test_projects_route.py tests/test_workspace.py tests/test_schedule_route.py tests/test_settings.py tests/test_static_js.py tests/test_api.py tests/test_mutation_specs.py -q`.
- [ ] **Step 3: Mutation specs** — run every new spec through the falsifier on a clean tree: `for s in overview-attention projects-index workspace-current schedule-route settings-route; do uv run python tools/falsify.py --spec tools/mutations/$s.json; done`. All CAUGHT.
- [ ] **Step 4:** `git diff --check` (no whitespace/conflict errors).
- [ ] **Step 5: Browser QA** — serve on a **dev port + temp dirs**: `BRIDGE_PORT=8788 BRIDGE_CONFIG=$TMP/config.toml uv run python -m bridge serve` against a temp DB seeded via the CLI/index (never `~/.bridge`). Drive Chrome (claude-in-chrome). For `/`, `/projects`, `/project/<id>` (each tab), `/schedule` (upcoming+history), `/diagnostics`, `/settings`, at **2560×1440, 1440×900, 1024×768, 768×1024, 375×812, 320px reflow, 200% zoom**, in **light and dark**, verify: keyboard-only operation + visible focus, screen-reader names/states (spot-check via DOM/aria), reduced motion, empty states, loading states, async failure, long project names/paths, missing metadata, dense realistic data, and dangerous permission selection. Confirm every visible control works and nav has no dead ends. Capture final screenshots for Overview, Project workspace, Schedule, Settings (light+dark).
- [ ] **Step 6: Re-run the full suite** after the last change. Commit any test-only additions. Leave the branch committed.

### Final checkpoint
- [ ] `git status --short --branch` clean except owner's untracked artifacts (`.DS_Store`, `.agents/`, `tools/.DS_Store`) which remain unstaged. Compose the final report (branch, HEAD, commits, files+behavior changed, exact verification commands+results, screenshot paths, limitations/deferred work).

---

## Deferred (explicitly out of scope — do not implement)

Editable machine configuration; full project-history counts/pagination/filtering/sorting; global command palette/shortcuts; new activity-event storage; new status taxonomy; remote access/auth; calendar-grid schedule view; onboarding beyond honest empty states + setup guidance.

## Self-review notes

- **Spec coverage:** Overview (M2.3), Projects (M2.5), Project workspace + tabs (M3), Schedule (M4.1–4.2), Diagnostics (M4.3), Settings (M5.1–5.2), shell/nav/tokens/typography/components (M1), token architecture + contrast (1.1), fonts (1.2), live-update preservation (2.4, 3.3), shared read models + macros (2.1, 2.2, and each model task), permission-never-persisted (5.2, 5.3), honest history limits (3.4), Schedule-only pagination (4.1), responsive/a11y (1.3, 5.4), verification (5.5) — each maps to a task.
- **No dead ends:** nav entries are added by the milestone that lands the route (Overview/Diagnostics in 1.3; Projects in 2.5; Schedule in 4.2; Settings in 5.2). Workspace is nested, not a global nav item.
- **Hook stability:** every task that touches a template preserves the `data-*` hook names the node-executed `test_static_js.py` and the mutation specs anchor on; renames (none planned) would migrate JS + harness + spec together in one commit.
