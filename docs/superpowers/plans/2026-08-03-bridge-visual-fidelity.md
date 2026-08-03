# Bridge Visual Fidelity Correction Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` and `superpowers:test-driven-development`. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reproduce the approved Overview and Project Current compositions while preserving Bridge's existing behavior, safety, routes, and progressive-enhancement hooks.

**Architecture:** Keep the current Jinja read models, vanilla JavaScript, token layers, and mutation endpoints. Make the Overview model expose only the three highest-priority visible attention items while retaining the exact total, then render one dominant item plus at most two compact secondary items above the Recent projects / Up next lower grid. Recompose Project Current around one bounded continuation card and two structured right-rail cards without replacing the authored prompt textarea or any stable `data-*` hook.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, vanilla JavaScript, CSS custom properties, pytest.

## Global Constraints

- The three approved HTML prototypes control visual composition; the committed redesign specification controls behavior, accessibility, safety, and data contracts.
- Preserve all routes, mutation endpoints, `data-*` hooks, textarea DOM identity, authored values, localhost-only authority, read-only Git access, scheduler/launcher/storage semantics, and non-persistent permission defaults.
- Reuse the existing palette, bundled fonts, and primitive → semantic → component token architecture.
- Do not touch `.DS_Store`, `.agents/`, `tools/.DS_Store`, `~/.bridge`, the LaunchAgent, port 8787, or origin.
- Browser QA uses only the disposable snapshot on port 8892.

---

### Task 1: Lock the approved structural contracts

**Files:** `tests/test_overview.py`, `tests/test_workspace.py`, `tests/test_shell.py`

- [ ] Add a model test proving the visible Overview subset is exactly three items while `attention_total` remains exact.
- [ ] Add route tests proving one `attention-primary`, no more than two `attention-secondary` cards, Projects and Schedule escape links, and Recent/Up next after the attention stage.
- [ ] Add Project Current tests proving the Next session heading, ready pill, node rail, nested saved-prompt surface, primary launch action, structured Project state facts, and Recent activity card.
- [ ] Add a shell test proving pages with real freshness data render Connected / Indexed state instead of generic footer copy.
- [ ] Run the focused tests and record the expected failures before production edits.

### Task 2: Restore the approved Overview composition

**Files:** `src/bridge/overview.py`, `src/bridge/templates/overview.html`, `src/bridge/templates/base.html`, `src/bridge/static/app.css`

- [ ] Change the visible attention limit to three after authoritative ranking, preserving the exact total and recent-project exclusion set.
- [ ] Render one dominant contextual attention card and up to two compact secondary cards in the approved `1.6fr / .85fr` stage.
- [ ] Render Recent projects and Up next as the lower grid, with explicit links to `/projects` and `/schedule`.
- [ ] Clamp long visible summaries while leaving full content available through project/schedule destinations.
- [ ] Render the connected/indexed sidebar footer when Overview freshness data supports it.
- [ ] Run focused Overview, shell, component, API, static-JS, and contrast suites; capture and compare a 1440×900 checkpoint.
- [ ] Commit as one logical Overview correction.

### Task 3: Restore the approved Project Current composition

**Files:** `src/bridge/templates/project.html`, `src/bridge/templates/_workspace_current.html`, `src/bridge/templates/_launch.html`, `src/bridge/static/app.css`

- [ ] Move breadcrumb, project path, and project actions into a contextual project header without changing hooks.
- [ ] Lead Current work with the Next session heading/copy and one bounded continuation card.
- [ ] Give the card a narrative title, ready pill, real node rail, nested Saved prompt surface, and visible primary launch row.
- [ ] Reorder Schedule, Copy prompt, Dismiss handoff, and Change options as secondary/tertiary controls while preserving every hook and textarea node.
- [ ] Replace the telemetry-first aside with structured Project state facts and Recent activity.
- [ ] Preserve the no-handoff Start session state and all disclosure/focus behavior.
- [ ] Run focused workspace, API, static-JS, shell, and contrast suites; capture and compare a 1440×900 checkpoint.
- [ ] Commit as one logical Project Current correction.

### Task 4: Regression hardening and final evidence

- [ ] Run every requested focused suite and all applicable redesign mutation specs.
- [ ] Run the full suite, then `git diff --check` and final status inspection.
- [ ] Browser-check light/dark at 1440×900, 1024×768, 768×1024, 375×812, 320px reflow, and 200% zoom.
- [ ] Check long summaries/paths, queued/no-handoff states, focus, Menu disclosure, prompt editing, Change options, Schedule disclosure, and dangerous permission styling without submitting mutations.
- [ ] Capture final Overview and Project Current screenshots beside approved references.
- [ ] Commit final test-only or fidelity corrections, then stop without push, merge, deploy, or service restart.

## Measured Baseline

- Approved Overview attention stage: 280px high; dominant card: 743px wide at 1440×900.
- Exact branch Overview: six equal rows in a 611px attention list.
- Approved Project Current continuation card: 381px high.
- Exact branch Project Current continuation card with long content: 529px high.
- Fresh baseline: 939 passed in 214.71 seconds at `7df0a2aef510d5883e0a94afb9d493f167de7e9b`.
