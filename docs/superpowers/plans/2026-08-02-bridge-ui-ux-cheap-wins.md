# Bridge UI/UX Cheap Wins Implementation Plan

> **Execution boundary:** Implement only the non-structural lane approved in the task and `docs/ux-audit.md`. Freshness architecture, history pagination/routes, card reordering, manual theming, and keyboard accelerators remain deferred.

**Goal:** Make Bridge's existing interface coherent, accessible, and reliable without changing its information architecture, sort model, or write boundaries.

**Architecture:** Preserve server-rendered Jinja pages and leaf-only SSE updates. Consolidate existing markup and presentation primitives, add focused behavior tests before JavaScript fixes, and keep all new visual semantics within the locked accent/warning color vocabulary.

**Tech stack:** Python 3.13, FastAPI, Jinja2, plain CSS, plain JavaScript, pytest, mutation specifications.

## Task 1: Keep live-status presentation synchronized

**Files:**
- Modify: `src/bridge/static/live.js`
- Modify: `src/bridge/static/app.css`
- Test: `tests/test_static_js.py`
- Mutation: `tools/mutations/task4-live-ui.json`

1. Add a failing JavaScript harness proving a live event updates both the visible word and the `.live--*` class without replacing the band.
2. Implement class synchronization and remove the inert `bridge:launched` listener.
3. Style the live telemetry line with explicit neutral/running states and non-color text/glyph cues.
4. Run targeted tests, the relevant mutation, and the full suite.

## Task 2: Give both Run now paths the same launch contract

**Files:**
- Modify: `src/bridge/templates/_card.html`
- Modify: `src/bridge/static/schedule.js`
- Test: `tests/test_static_js.py`
- Mutation: `tools/mutations/task6-scheduled-runs.json`

1. Add failing harnesses proving compose Run now sends model, effort, and permission and copies the prompt on every failed launch.
2. Associate the compose action with the existing launch controls without duplicating their values.
3. Preserve the prompt on failure and clear it only after a confirmed launch.
4. Run targeted tests, the relevant mutation, and the full suite.

## Task 3: Establish the compact instrument-panel visual system

**Files:**
- Modify: `src/bridge/static/app.css`
- Modify: `src/bridge/templates/base.html`
- Modify: `src/bridge/templates/dashboard.html`
- Modify: `src/bridge/templates/project.html`
- Modify: `src/bridge/templates/diagnostics.html`
- Add: `src/bridge/static/favicon.svg`
- Test: `tests/test_api.py`
- Test: `tests/test_contrast.py`

1. Add markup assertions for one page heading, labeled home navigation, visible Pin copy, metadata, and table scroll containers.
2. Add semantic spacing, typography, radius, control, surface, and status tokens using lowercase six-digit hex colors.
3. Style every shipped class named in the audit, neutralize queued/addition/deletion colors, and make totals, headers, controls, and tables reflow at narrow widths and text zoom.
4. Add the favicon and page metadata.
5. Prove both themes' contrast and run the full suite.

## Task 4: Remove duplicate UI primitives without behavior change

**Files:**
- Modify: `src/bridge/templates/_card.html`
- Modify: `src/bridge/static/copy.js`
- Modify: `src/bridge/static/launch.js`
- Modify: `src/bridge/static/projects.js`
- Modify: `src/bridge/static/schedule.js`
- Test: `tests/test_api.py`
- Test: `tests/test_static_js.py`

1. Extract the repeated schedule form into one Jinja macro while preserving rendered hooks and mutation anchors.
2. Define one global live-region announcer and use it from all action scripts.
3. Share form-control styling through selectors rather than changing hooks.
4. Run focused tests and the full suite.

## Task 5: Make existing detail states honest

**Files:**
- Modify: `src/bridge/templates/project.html`
- Test: `tests/test_api.py`

1. Add compact empty-state copy for project history sections only.
2. State that each table shows the latest 50 records when a full result reaches the current server cap.
3. Do not add empty card affordances or imply that older records are reachable until pagination is approved.
4. Run focused tests and the full suite.

## Final verification

1. Run `uv run pytest -q` and confirm all tests pass.
2. Run mutation verification for each changed behavior and confirm every new behavior is caught.
3. Inspect light/dark rendered HTML and the live localhost service at narrow and wide viewport equivalents; use direct DOM/CSS inspection if the in-app browser remains unavailable.
4. Confirm `git diff --check`, mutation anchor integrity, clean worktree, imperative commit messages, and no AI-attribution trailers.
