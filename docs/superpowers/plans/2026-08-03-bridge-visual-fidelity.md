# Bridge Visual Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the implemented Bridge redesign back to the approved “structural calm” composition without changing its backend, routes, mutation contracts, or progressive-enhancement behavior.

**Architecture:** Keep the existing server-rendered Jinja, vanilla JavaScript, read models, and three-layer token system. Correct the experience through explicit page composition classes and small semantic markup changes; the only read-model behavior change is a fixed cap on Overview attention items so the landing page remains a glanceable subset rather than a second Projects index.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, vanilla ES, CSS custom properties, pytest, Node-executed static-JS tests.

## Global Constraints

- `docs/superpowers/specs/2026-08-02-bridge-product-redesign-design.md` remains the product and visual authority.
- Preserve every existing route, mutation endpoint, `data-*` JavaScript hook, textarea identity guarantee, local-only authority boundary, and permission-mode safety rule.
- Preserve Atkinson Hyperlegible Next for interface copy and IBM Plex Mono for paths, IDs, timestamps, branches, token values, and prompt previews.
- Use the existing primitive → semantic → component token architecture. Components do not introduce raw colors.
- Desktop controls are at least 40px high; narrow/touch controls remain at least 44px high.
- Radii use 6, 8, 10, or 12 pixels. Permanent surfaces use rules rather than floating shadows.
- The Overview contains only a highest-value subset. Projects remains the complete index.
- The real `Session ended → Handoff ready → Next session` span is Bridge’s signature element and appears only when those states exist.
- Automated tests establish structural and behavioral contracts; browser screenshots establish visual fidelity. Do not use brittle pixel-golden tests.
- Preserve the owner’s untracked `.DS_Store`, `.agents/`, and `tools/.DS_Store` files and do not stage them.

---

### Task 1: Shared visual foundation and Bridge identity

**Files:**
- Modify: `src/bridge/static/app.css`
- Modify: `src/bridge/templates/base.html`
- Modify: `src/bridge/templates/_shell.html`
- Modify: `tests/test_shell.py`

**Interfaces:**
- Produces `.bridge-mark`, `.shell-status`, and corrected `.btn` geometry for every later page.
- Preserves `#primary-nav`, `.menu-toggle`, `data-freshness-strip`, and all existing navigation URLs.

- [ ] **Step 1: Write failing shell and CSS contract tests.** Assert that the shell renders an SVG structural mark with `class="bridge-mark"`, that button-like anchors remove text decoration and use inline-flex alignment, that `--control-min` is `2.5rem`, and that `--radius-sm` is at least `6px`.

```python
def test_shell_renders_bridge_span_mark(tmp_path):
    html = _client(tmp_path).get("/").text
    assert 'class="bridge-mark"' in html
    assert 'aria-hidden="true"' in html

def test_visual_foundation_uses_approved_control_scale():
    css = (STATIC / "app.css").read_text()
    assert "--control-min: 2.5rem" in css
    assert "--radius-sm: 6px" in css
    assert ".btn {" in css and "text-decoration: none" in css
```

- [ ] **Step 2: Run `uv run pytest tests/test_shell.py -q` and confirm the new assertions fail for the missing mark and old 32px/4px tokens.**
- [ ] **Step 3: Add the structural bridge mark beside the wordmark, a quiet sidebar status slot where page data permits it, 40px desktop controls, 6px minimum radii, and finished anchor-button alignment.** Keep the dark rail and current nav contracts.
- [ ] **Step 4: Run `uv run pytest tests/test_shell.py tests/test_contrast.py tests/test_static_js.py -q`.**
- [ ] **Step 5: Browser-check the shell at 1280×720 and 375×812 in light and dark themes.**
- [ ] **Step 6: Commit with `git commit -m "Refine Bridge shell identity and control scale"`.**

### Task 2: Bounded, scannable Overview

**Files:**
- Modify: `src/bridge/overview.py`
- Modify: `src/bridge/templates/overview.html`
- Modify: `src/bridge/static/app.css`
- Modify: `tests/test_overview.py`
- Modify: `tests/test_components.py` only if a compact row macro contract is shared

**Interfaces:**
- Produces `ATTENTION_LIMIT = 6` and `OverviewModel.attention` containing at most six already-ranked attention items.
- Produces `.overview-layout`, `.attention-list`, and `.attention-row` without changing existing action URLs or live-update hooks.

- [ ] **Step 1: Write a failing model test with eight ranked attention cards and assert only the first six survive in authoritative card order.**

```python
def test_overview_caps_attention_to_one_glance(tmp_path):
    model = build_overview(store, cfg, cards=cards_with_eight_attention_items, now=NOW)
    assert len(model.attention) == 6
    assert [item.project_id for item in model.attention] == expected_ranked_ids[:6]
```

- [ ] **Step 2: Write a failing route test asserting `overview-layout`, `attention-list`, a compact row for every rendered attention item, and a `View all projects` escape immediately adjacent to the attention group.**
- [ ] **Step 3: Run the two focused tests and confirm they fail for the unbounded model and generic card markup.**
- [ ] **Step 4: Add `ATTENTION_LIMIT = 6`, apply it after combining project and schedule attention in authoritative order, and recompose the page into a primary attention column plus a quiet Recent/Up Next secondary stack.** Truncate verbose handoff summaries visually to two lines without altering stored content or accessible action labels.
- [ ] **Step 5: Move the eight-metric dashboard strip out of the page-heading role. Keep freshness and Refresh in the header; render a quiet absolute-value usage line at the foot and only the minimal counts needed for orientation.**
- [ ] **Step 6: Run `uv run pytest tests/test_overview.py tests/test_components.py tests/test_api.py tests/test_static_js.py -q`.**
- [ ] **Step 7: Browser-check populated and empty Overview states at 1440×900, 1024×768, and 375×812. Confirm Recent Projects is reachable within roughly one desktop viewport with six attention items.**
- [ ] **Step 8: Commit with `git commit -m "Recompose Overview as a bounded attention surface"`.**

### Task 3: Project Current as the signature continuation surface

**Files:**
- Modify: `src/bridge/templates/_workspace_current.html`
- Modify: `src/bridge/templates/_launch.html`
- Modify: `src/bridge/static/app.css`
- Modify: `tests/test_workspace.py`
- Modify: `tests/test_static_js.py` only where markup movement affects the existing harness

**Interfaces:**
- Produces `.workspace-current`, `.continuation-panel`, and a three-node `.workspace-span` with the visible labels `Session ended`, `Handoff ready`, and `Next session`.
- Keeps `data-handoff-section`, `data-prompt-handoff`, `data-launch`, `data-launch-button`, `data-schedule-toggle`, and `data-handoff-dismiss` unchanged.

- [ ] **Step 1: Write failing workspace tests asserting the three fixed span labels, continuation-before-project-state source order, and that the single `.btn--primary` is inside the continuation panel.**

```python
def test_current_tab_renders_the_real_bridge_span(client, seeded_project):
    html = client.get(f"/project/{seeded_project}?tab=current").text
    assert "Session ended" in html
    assert "Handoff ready" in html
    assert "Next session" in html
    assert html.index("continuation-panel") < html.index("workspace-state")
```

- [ ] **Step 2: Run the focused tests and confirm they fail against the current prose-only span and reversed hierarchy.**
- [ ] **Step 3: Reorder Current so the handoff continuation is primary and project state is secondary. Replace the duplicated summary sentence with three concise connected states, keep the saved prompt preview to two or three lines, and place Continue in Terminal plus Schedule in the panel footer.** Copy/Edit/Dismiss remain available but tertiary.
- [ ] **Step 4: At wide viewports, use a purposeful main/secondary grid. Collapse to one source-ordered column below 1024px. The primary button remains 40px/44px minimum and launch options remain one labeled disclosure level deep.**
- [ ] **Step 5: Run `uv run pytest tests/test_workspace.py tests/test_static_js.py tests/test_api.py -q`.**
- [ ] **Step 6: Browser-check queued-handoff and no-handoff projects at desktop, tablet, mobile, light, and dark. Exercise Edit prompt, Change options, and Start a different session without submitting mutations.**
- [ ] **Step 7: Commit with `git commit -m "Restore the signature project continuation surface"`.**

### Task 4: Screen-specific supporting-page composition

**Files:**
- Modify: `src/bridge/templates/_components.html`
- Modify: `src/bridge/templates/projects.html`
- Modify: `src/bridge/templates/schedule.html`
- Modify: `src/bridge/templates/diagnostics.html`
- Modify: `src/bridge/templates/settings.html`
- Modify: `src/bridge/static/app.css`
- Modify: `tests/test_components.py`
- Modify: `tests/test_projects_route.py`
- Modify: `tests/test_schedule_route.py`
- Modify: `tests/test_settings.py`
- Modify: `tests/test_api.py` for Diagnostics structural assertions

**Interfaces:**
- `project_summary_row(summary)` retains the same content and `/project/{id}` action but gains stable identity/activity/action wrappers.
- Schedule, Diagnostics, and Settings retain all existing data hooks, form values, and read-only guarantees.

- [ ] **Step 1: Write failing component and route tests for project-row subregions, a single useful Schedule empty surface when all upcoming groups are empty, a non-duplicated healthy Diagnostics state, and constrained Settings preference groups.**
- [ ] **Step 2: Run the focused test set and verify the structural assertions fail.**
- [ ] **Step 3: Recompose Projects into identity, activity, and trailing-action columns with paths on their own mono line. Keep search/filter behavior and secondary action disclosures unchanged.**
- [ ] **Step 4: Recompose Settings into bounded Display and Launch Defaults groups, constrain selects to readable widths, and render effective configuration as quiet key/value rows with mono values.**
- [ ] **Step 5: Replace Schedule’s three repetitive empty messages with one agenda empty state that explains scheduling begins from a project. Populated groups retain their current order and controls.**
- [ ] **Step 6: When Diagnostics is healthy, show one compact healthy summary and omit the redundant empty Needs attention group. Degraded states continue to show cause and next action before routine facts.**
- [ ] **Step 7: Run `uv run pytest tests/test_components.py tests/test_projects_route.py tests/test_schedule_route.py tests/test_settings.py tests/test_api.py tests/test_static_js.py -q`.**
- [ ] **Step 8: Browser-check all four pages at desktop and mobile widths, including dense Projects and long configuration paths.**
- [ ] **Step 9: Commit with `git commit -m "Give Bridge supporting pages purposeful composition"`.**

### Task 5: Full verification and visual checkpoint artifact set

**Files:**
- Create: `docs/superpowers/plans/assets/redesign-fidelity/*.png` (kept local if ignored)
- Modify: this plan only to check completed steps if the execution workflow tracks them in-file

**Interfaces:**
- Produces current screenshots for Overview, Project Current, Projects, Schedule, Diagnostics, and Settings.

- [ ] **Step 1: Run `uv run pytest -q` and record the exact count and duration.**
- [ ] **Step 2: Run every redesign mutation spec through `uv run python tools/falsify.py --spec ...`; every mutation must be CAUGHT.**
- [ ] **Step 3: Run `git diff --check` and inspect `git status --short` for owner-file preservation.**
- [ ] **Step 4: Browser QA at 1440×900, 1024×768, 768×1024, 375×812, 320px reflow, and 200% zoom, in light and dark. Check focus, Menu disclosure, empty states, long content, and dangerous-permission styling.**
- [ ] **Step 5: Capture the final core-screen screenshots and compare them directly against the approved visual-system requirements: quiet structural surfaces, bounded hierarchy, work blue only for work/focus, rust only for risk, and the bridge span as the single signature moment.**
- [ ] **Step 6: Commit any final test-only or fidelity corrections as one logical commit, then report the remaining limitations without claiming broader coverage than was executed.**

## Self-review

- Spec coverage: shell identity, Overview hierarchy, continuation signature, supporting-page composition, scale, responsive behavior, dark/light themes, and browser QA all map to explicit tasks.
- Scope: no backend, storage, scheduling, launcher, or authority redesign is included.
- Contract preservation: every existing route and named `data-*` hook remains in place; markup only moves around those stable anchors.
- Placeholder scan: no TBD/TODO or undefined production interface remains.
