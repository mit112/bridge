# Bridge Almanac Overview — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the warm-editorial "Almanac" visual system on Bridge's Overview page (light + dark), re-theming the shared token/type foundation so later pages inherit it.

**Architecture:** This is a **re-theme + targeted additions**, not a rewrite. The palette and type changes happen at the primitive/semantic layers of the existing three-layer `app.css` token system, so they cascade to every page automatically (teal→terracotta, cool-gray→cream, sans headings→Fraunces serif). The *new structural components* — the at-a-glance command strip, status-top-border attention cards, editorial recent-project rows, numbered sections, double-rule masthead — are scoped to the Overview template and its CSS. One new backend datum (`dirty` tree count) is added to the already-computed topbar envelope. No route, IA, or data-flow changes.

**Tech Stack:** FastAPI + Jinja2 (server-rendered), single hand-authored `src/bridge/static/app.css`, vanilla per-page JS, self-hosted woff2 fonts. No build system, no framework, no npm. Tests: `pytest` (run with `uv run pytest`).

## Global Constraints

Every task's requirements implicitly include this section. Copied from the spec (`docs/superpowers/specs/2026-08-03-bridge-almanac-visual-redesign-design.md`).

- **WCAG 2.2 AA in both themes.** Every text pair ≥ 4.5:1. Non-text boundaries that are the *sole* means of identifying an interactive control (form-control edges, focus rings) ≥ 3:1 — held by `--p-field-line` and `--p-nav-focus`, which the suite tests. **Decorative hairline rules (`--rule`/`--line`) are intentionally lighter than 3:1** and are WCAG-exempt: they separate content whose identity/state is already carried by layout, labels, and (for status) a colored top-border + tag — never by the hairline alone. This matches the currently shipping design, where `--rule`/`--line` are already ~1.1:1 by design. `tests/test_contrast.py` is the gate and must stay green — never weaken it, only extend its coverage.
- **`tests/test_contrast.py` parses PRIMITIVE tokens only** (`--p-*`, six-digit lowercase hex). It splits the stylesheet at the **first** literal occurrence of the string `prefers-color-scheme: dark`: everything before = light theme, everything after = dark. Keep all light primitives before that line and all dark primitives after it, and never let that exact substring appear earlier (e.g. in a comment).
- **Status is never color-only** (WCAG 1.4.1): every status carries a text label + a mono slug + color (top-border/tag). See spec §5.4.
- **Fonts self-hosted, no CDN.** New faces ship as woff2 under `src/bridge/static/fonts/` with an OFL license file beside them and a `PROVENANCE.md` entry (SHA-256 + source). `tests/test_fonts.py` enforces CDN-absence + font-file/OFL presence today; Task 1 extends it with a Fraunces provenance-entry check.
- **Icons are real SVG with accessible names.** The `◧ ▦ ▤ ✳ ⚙` glyphs in the mockups are placeholders — never ship unicode/emoji as icons; reuse the existing `_shell.html` inline-SVG nav approach.
- **No new runtime dependency** beyond the Fraunces font files. No build system, no framework, no npm/package.json.
- **Token discipline:** components reference the semantic layer (or the legacy aliases), never a primitive or a raw hex. New primitives are added at the primitive layer; new component slots point at semantics.
- **Spacing on the existing 4px scale** (`--space-1..10`); reuse `--radius-*`, `--motion-fast`/`--motion`. Honor `prefers-reduced-motion: reduce`. One primary (filled) action per view.
- **No AI attribution** in commit messages, branches, or tags.

**Verified starting palette** (design intent — the contrast suite is the final arbiter; adjust any pair it flags, keeping the intent):

Light (cream):
```
--p-canvas:  #f5efe3   --p-surface: #fcf8f0
--p-text:    #211d17   --p-text-2:  #6b6152   --p-rule: #dcd2bf
--p-work:    #a23c17   --p-work-soft: #f0e0d5     (terracotta accent / interactive / needs-input; darkened from the mockup's #b0421d, which is only 4.48:1 on --p-work-soft — below the 4.5 the existing test_contrast.py:30 pair enforces)
--p-run:     #2f6b46   --p-run-soft:  #e1ece3     (forest — running / live / success)
--p-review:  #7e5a10   --p-review-soft: #f0e6cf   (gold — NEEDS-REVIEW; darkened from the mockup's #9a6a14, which fails 4.5:1 as text on cream)
--p-risk:    #99301a   --p-risk-soft: #f2ddd2     (deep rust — genuine failure/stale, distinct from the accent)
--p-field-line: #867a64                            (form-control border, 3:1 floor)
--p-nav:     #1e1a14   (rail — dark espresso in BOTH themes)
--p-nav-text: #f7efe0  --p-nav-text-2: #b7ab95  --p-nav-accent: #d8a07f  --p-nav-focus: #e6b89b
```
Dark ("night almanac" — espresso, NOT an inversion):
```
--p-canvas:  #1b1714   --p-surface: #241f1a   --p-raised: #2c261f
--p-text:    #efe6d6   --p-text-2:  #b3a691   --p-rule: #3a332a
--p-work:    #db7048   --p-work-soft: #301f16     (soft darkened from the mockup's #39251b, which is only 4.34:1 under --p-work — below 4.5)
--p-run:     #74b98d   --p-run-soft:  #22322a
--p-review:  #cf9a3f   --p-review-soft: #322a18
--p-risk:    #e19a7e   --p-risk-soft: #402820
--p-field-line: #7a6f5e
--p-nav:     #1e1a14   --p-nav-text: #f7efe0  --p-nav-text-2: #b7ab95  --p-nav-accent: #d8a07f  --p-nav-focus: #e6b89b
```
**Recompute every pair before claiming Task 2 green.** The two accent/soft pairs above are corrected here (light `--p-work` on `--p-work-soft` → ~5.0:1; dark `--p-work` on `--p-work-soft` → ~4.8:1). The remaining tight ones to watch: dark `--p-work` on `--p-surface` (~4.7:1), light `--p-run` on `--p-run-soft` (~5.2:1), light `--p-review` on `--p-review-soft` (~5.0:1). Nudge the primitive (not the hue) if the suite flags any.

**Out of scope for this plan** (tracked for later, do NOT build here): wiring the command strip to `live.js` partial updates (v1 strip is server-rendered, refreshed on full page load); restyling Projects / Schedule / Settings / Diagnostics / project-detail layouts (they inherit the palette + type cascade but keep their current structure until their own rollout).

---

### Task 1: Bundle the Fraunces display font

**Files:**
- Create: `src/bridge/static/fonts/fraunces-semibold-600.woff2` (fetched binary)
- Create: `src/bridge/static/fonts/fraunces-italic-400.woff2` (fetched binary)
- Create: `src/bridge/static/fonts/OFL-Fraunces.txt`
- Modify: `src/bridge/static/fonts/PROVENANCE.md` (add a Fraunces section)
- Modify: `src/bridge/static/app.css` (add `@font-face` blocks + `--font-display` token)
- Modify: `src/bridge/templates/base.html:31` (preload the display face before the stylesheet link)
- Test: `tests/test_fonts.py`

**Interfaces:**
- Produces: CSS font-family `"Fraunces"` (weight 600 normal; weight 400 italic) and the token `--font-display: "Fraunces", Georgia, "Times New Roman", serif;` — consumed by Task 3's type treatment.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_fonts.py`:

```python
def test_fraunces_display_font_present():
    fonts = STATIC / "fonts"
    assert (fonts / "OFL-Fraunces.txt").exists()
    assert list(fonts.glob("fraunces-*.woff2"))
    # Provenance is recorded, not just the bytes dropped in (matches the
    # convention the existing fonts follow — a source + SHA-256 per file).
    provenance = (fonts / "PROVENANCE.md").read_text()
    assert "Fraunces" in provenance
    assert "fraunces-semibold-600.woff2" in provenance


def test_fraunces_declared_in_css():
    assert 'font-family: "Fraunces"' in CSS
    assert "/static/fonts/fraunces-" in CSS
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_fonts.py::test_fraunces_display_font_present tests/test_fonts.py::test_fraunces_declared_in_css -v`
Expected: FAIL (files missing, "Fraunces" not in CSS).

- [ ] **Step 3: Fetch the two woff2 static instances + OFL, record provenance**

Fraunces is SIL OFL 1.1 (Undercase Type, https://github.com/undercasetype/Fraunces). Obtain two **static** woff2 instances — SemiBold (weight 600, roman) and an Italic (weight 400) at a display optical size — from the official upstream (or a faithful self-host packager). Save them under `src/bridge/static/fonts/` as exactly:
- `fraunces-semibold-600.woff2`
- `fraunces-italic-400.woff2`

Copy the upstream `OFL.txt` verbatim to `src/bridge/static/fonts/OFL-Fraunces.txt`. Then append a Fraunces section to `PROVENANCE.md` matching the existing table format (Source URL, commit/release anchor, license file name, and a `| File | Source path | SHA-256 |` row per woff2). Compute each hash with `shasum -a 256 <file>`.

- [ ] **Step 4: Declare the faces + token in `app.css`**

After the existing `@font-face` blocks (the IBM Plex Mono ones ending near `app.css:202`), add:

```css
@font-face {
  font-family: "Fraunces";
  src: url("/static/fonts/fraunces-semibold-600.woff2") format("woff2");
  font-weight: 600;
  font-style: normal;
  font-display: swap;
}
@font-face {
  font-family: "Fraunces";
  src: url("/static/fonts/fraunces-italic-400.woff2") format("woff2");
  font-weight: 400;
  font-style: italic;
  font-display: swap;
}
```

In the `:root` block that defines `--font-sans`/`--font-mono` (`app.css:203-206`), add:

```css
  --font-display: "Fraunces", Georgia, "Times New Roman", serif;
```

- [ ] **Step 5: Preload the display face in `base.html`**

Immediately before `<link rel="stylesheet" href="/static/app.css">` (`base.html:31`), add:

```html
  <link rel="preload" href="/static/fonts/fraunces-semibold-600.woff2" as="font" type="font/woff2" crossorigin>
```

- [ ] **Step 6: Run the font tests + the full suite**

Run: `uv run pytest tests/test_fonts.py -v`
Expected: PASS (incl. the two new tests and the existing `test_font_face_uses_swap`, now counting ≥ 2 `font-display: swap`).
Run: `uv run pytest`
Expected: PASS (no regressions).

- [ ] **Step 7: Commit**

```bash
git add src/bridge/static/fonts/ src/bridge/static/app.css src/bridge/templates/base.html tests/test_fonts.py
git commit -m "Bundle Fraunces display font for the Almanac redesign"
```

---

### Task 2: Re-theme the primitive palette (light + dark) + add status primitives

**Files:**
- Modify: `src/bridge/static/app.css:4-157` (the four primitive blocks: `:root`, `:root[data-theme="light"]`, `@media (prefers-color-scheme: dark) :root`, `:root[data-theme="dark"]`) and the semantic/legacy/component token blocks (`app.css:34-87`)
- Modify: `src/bridge/templates/base.html:7-8` (browser-chrome `theme-color` meta)
- Test: `tests/test_contrast.py`

**Interfaces:**
- Consumes: nothing.
- Produces: warm primitives (all existing names retained, re-valued) plus new primitives `--p-run`, `--p-run-soft`, `--p-review`, `--p-review-soft`; new semantic tokens `--status-running`, `--status-review`, `--status-attention`, `--run-soft`, `--review-soft`; new component tokens `--pill-run-bg/-fg`, `--pill-review-bg/-fg`. `--work`/`--accent`/`--focus` now resolve to terracotta (`--p-work`). Consumed by every later task and, via cascade, every page.

**Why keep the primitive names:** the contrast suite and all component CSS reference `--p-work`, `--p-risk`, `--p-work-soft`, `--p-risk-soft`, `--p-field-line`, `--p-nav*` by name. Renaming would break the suite's `assert fg in tokens` and orphan components. So the accent shift is done by **re-valuing** `--p-work` to terracotta (it keeps its name); we only **add** the genuinely new status colors.

- [ ] **Step 1: Write the failing test — extend the contrast pairs**

In `tests/test_contrast.py`, add these entries to the `PAIRS` list (after the existing `--p-work`/`--p-risk` rows):

```python
    ("--p-run", "--p-surface", 4.5, "running status text on a surface"),
    ("--p-run", "--p-canvas", 4.5, "running status text on canvas"),
    ("--p-run", "--p-run-soft", 4.5, "running tag text on its soft pill"),
    ("--p-review", "--p-surface", 4.5, "review status text on a surface"),
    ("--p-review", "--p-canvas", 4.5, "review status text on canvas"),
    ("--p-review", "--p-review-soft", 4.5, "review tag text on its soft pill"),
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_contrast.py -v`
Expected: FAIL — `--p-run is not defined` / `--p-review is not defined` (the primitives don't exist yet).

- [ ] **Step 3: Re-value the light primitive blocks**

Replace the primitive tokens in the default `:root` block (`app.css:9-32`) AND the identical `:root[data-theme="light"]` block (`app.css:92-108`) with the light palette. Both blocks carry the same values (the attribute selector just wins over `prefers-color-scheme`). Per block:

```css
  --p-nav: #1e1a14;
  --p-canvas: #f5efe3;
  --p-surface: #fcf8f0;
  --p-text: #211d17;
  --p-text-2: #6b6152;
  --p-rule: #dcd2bf;
  --p-work: #a23c17;
  --p-work-soft: #f0e0d5;
  --p-run: #2f6b46;
  --p-run-soft: #e1ece3;
  --p-review: #7e5a10;
  --p-review-soft: #f0e6cf;
  --p-risk: #99301a;
  --p-risk-soft: #f2ddd2;
  --p-field-line: #867a64;
  --p-nav-text: #f7efe0;
  --p-nav-text-2: #b7ab95;
  --p-nav-accent: #d8a07f;
  --p-nav-focus: #e6b89b;
```

(Keep the surrounding comments; just update the hex. The `:root[data-theme="light"]` block also keeps its `--surface-raised: var(--p-surface);` line.)

- [ ] **Step 4: Re-value the dark primitive blocks**

Replace the primitives in the `@media (prefers-color-scheme: dark) :root` block (`app.css:112-136`) AND the identical `:root[data-theme="dark"]` block (`app.css:139-157`) with the dark palette. Both carry the same values and keep `--surface-raised: var(--p-raised);`:

```css
    --p-nav: #1e1a14;
    --p-canvas: #1b1714;
    --p-surface: #241f1a;
    --p-raised: #2c261f;
    --p-text: #efe6d6;
    --p-text-2: #b3a691;
    --p-rule: #3a332a;
    --p-work: #db7048;
    --p-work-soft: #301f16;
    --p-run: #74b98d;
    --p-run-soft: #22322a;
    --p-review: #cf9a3f;
    --p-review-soft: #322a18;
    --p-risk: #e19a7e;
    --p-risk-soft: #402820;
    --p-field-line: #7a6f5e;
    --p-nav-text: #f7efe0;
    --p-nav-text-2: #b7ab95;
    --p-nav-accent: #d8a07f;
    --p-nav-focus: #e6b89b;
```

Guard: the string `prefers-color-scheme: dark` must still first appear at the `@media` line (not in any comment above it), or the contrast parser will split the file at the wrong point.

- [ ] **Step 5: Add the new status tokens to the semantic + component layers**

In the semantic block (`app.css:34-53`), after the `--risk-soft` line, add:

```css
  --run: var(--p-run);
  --run-soft: var(--p-run-soft);
  --review: var(--p-review);
  --review-soft: var(--p-review-soft);
  --status-running: var(--p-run);
  --status-review: var(--p-review);
  --status-attention: var(--p-work);
```

In the component block (`app.css:75-87`), after the `--pill-risk-*` lines, add:

```css
  --pill-run-bg: var(--run-soft); --pill-run-fg: var(--run);
  --pill-review-bg: var(--review-soft); --pill-review-fg: var(--review);
```

- [ ] **Step 6: Update the browser-chrome `theme-color` to the new canvas**

In `base.html` (`base.html:7-8`), the two `<meta name="theme-color">` values still point at the old teal-era canvas. Update them to the Almanac canvas so the mobile browser chrome matches:

```html
  <meta name="theme-color" content="#f5efe3" media="(prefers-color-scheme: light)">
  <meta name="theme-color" content="#1b1714" media="(prefers-color-scheme: dark)">
```

- [ ] **Step 7: Run the contrast suite, then the full suite**

Run: `uv run pytest tests/test_contrast.py -v`
Expected: PASS in both themes for all pairs. If any flagged pair (dark `--p-work`, light `--p-run`/`--p-review` on their soft pills) is below floor, nudge that primitive darker (light) or lighter (dark) by a few points and re-run — keep the hue.
Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 8: Render a spot-check (palette cascade)**

Restart the local panel on `127.0.0.1:8787` (authorized — see memory `bridge-serve-restart-authorized`). Headless-screenshot the Overview in light and dark and Read the PNGs to confirm the warm palette + terracotta accent now cascade (layout unchanged this task):

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu \
  --hide-scrollbars --window-size=1440,1700 \
  --screenshot=/private/tmp/claude-501/-Users-mitsheth-dev-bridge/5d71d7f5-8ec9-49b1-90f3-e46fd7943d04/scratchpad/overview-palette-light.png \
  "http://127.0.0.1:8787/"
```
(For dark: set `localStorage['bridge.appearance']='dark'` via the Claude-in-Chrome extension if connected, otherwise capture dark via the same headless call with the OS in dark appearance. Theme fidelity is re-verified in Task 8.)

- [ ] **Step 9: Commit**

```bash
git add src/bridge/static/app.css src/bridge/templates/base.html tests/test_contrast.py
git commit -m "Re-theme primitives to the Almanac warm palette with status roles"
```

---

### Task 3: Almanac type + masthead treatment (global cascade)

**Files:**
- Modify: `src/bridge/static/app.css` — `.page-head` (366-377), `.page-title` (376), `.page-lede` (1288), add `.page-eyebrow`, `.card__head h2` (398), `.attention-primary h3` (1002)
- Modify: `src/bridge/templates/overview.html:4` — add the `page_eyebrow` block
- Test: visual (render) + full suite green (no contrast regression)

**Interfaces:**
- Consumes: `--font-display` (Task 1), warm primitives (Task 2).
- Produces: the display-serif masthead + card-title identity used by all pages (serif at ≥19px only, per spec §4); the Overview eyebrow. Section labels (Task 6) and the Overview recent-row names (Task 7) are typed in their own tasks.

- [ ] **Step 1: Apply the display serif to the masthead + heading slots**

Edit `.page-title` (`app.css:376`) to:

```css
.page-title {
  margin: 0;
  font: 600 clamp(2rem, 1.2rem + 2.6vw, 2.75rem)/1.05 var(--font-display);
  letter-spacing: -.015em;
}
```

Edit `.page-head` (`app.css:366-375`) to close with a double rule and hold the eyebrow/title/lede in a column — add these declarations to the existing rule (keep its width/margin/padding/flex):

```css
  border-bottom: 3px double var(--text);
  align-items: flex-end;
```

Edit `.page-lede` (`app.css:1288`) to the italic serif subtitle:

```css
.page-lede {
  margin: var(--space-2) 0 0;
  color: var(--text-secondary);
  font: italic 400 var(--text-title)/1.35 var(--font-display);
}
```

Add a new rule near `.page-head` for the eyebrow:

```css
.page-eyebrow {
  margin: 0 0 var(--space-2);
  color: var(--work);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  letter-spacing: .2em;
  text-transform: uppercase;
}
```

Point the **display-sized** heading slots at the serif. Per spec §4, the serif sets only titles ≈19px and up — smaller UI text stays Atkinson. So each slot below gets `--font-display` **and** a size at/above the threshold:

- `.card__head h2` (`app.css:398`) — currently `700 var(--text-title)/1.25 var(--font-sans)`; `--text-title` is 1.25rem (20px), already ≥19px. Change to `600 var(--text-title)/1.25 var(--font-display)`.
- `.attention-primary h3` (the hero title, `app.css:1002`) — currently `700 1.125rem` (18px, just under the threshold). Bump and serif it: `font: 600 1.375rem/1.3 var(--font-display); letter-spacing: -.01em;`.
- **Do NOT serif `.attention-secondary h3`** (`app.css:1046`): it is 15px, below the display threshold, and has no font-family/weight declaration to "swap" — it stays Atkinson. Leave it untouched here.
- **Do NOT serif `.overview-attention h2, .overview-panel h2`** (`app.css:961-962`): Task 6 restyles these into the small uppercase section label (Atkinson), not serif.
- **`.project-row__name` is handled in Task 7, not here.** On Projects it stays 16px Atkinson (below threshold, dense list); only the Overview `.overview-recent` copy is bumped to 20px + serif (Task 7, scoped).

- [ ] **Step 2: Add the Overview eyebrow**

In `overview.html`, after `{% block page_title %}Overview{% endblock %}` (`overview.html:4`), add:

```jinja
{% block page_eyebrow %}<p class="page-eyebrow">Local control plane</p>{% endblock %}
```

- [ ] **Step 3: Verify the suite stays green**

Run: `uv run pytest`
Expected: PASS (type changes touch no measured contrast pair; `test_shell.py`/`test_overview.py` render assertions still hold — the eyebrow is additive markup).

- [ ] **Step 4: Render + visual check**

Restart the panel; headless-screenshot `/` (light) and Read the PNG. Confirm: mono terracotta eyebrow, large Fraunces "Overview", italic serif subtitle, double-rule under the masthead. Compare against `scratchpad/mock-C2-light.html` render.

- [ ] **Step 5: Commit**

```bash
git add src/bridge/static/app.css src/bridge/templates/overview.html
git commit -m "Apply Almanac display-serif type and double-rule masthead"
```

---

### Task 4: Add the `dirty` tree count to the topbar envelope

**Files:**
- Modify: `src/bridge/dashboard.py:148-160` (the `topbar` dict)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `cards` (each has `card.git.dirty_count`).
- Produces: `envelope["topbar"]["dirty"]` — an int count of cards with uncommitted changes. Flows unchanged into `OverviewModel.totals` (built from `envelope["topbar"]`) and is consumed by Task 5's command strip.

- [ ] **Step 1: Write the failing test**

Find the existing topbar assertion in `tests/test_dashboard.py` (near line 39, the test that builds an update and asserts `topbar["today"]`). In that same test, after the existing topbar asserts, add an assertion that `dirty` counts cards with a non-zero `dirty_count`. If that test's fixture has no dirty card, add one dirty card to the fixture and assert:

```python
    assert update["topbar"]["dirty"] == 1  # one card has uncommitted changes
```

(Match the fixture's actual dirty-card count; the point is a non-default, asserted value.)

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_dashboard.py -k topbar -v`
Expected: FAIL — `KeyError: 'dirty'`.

- [ ] **Step 3: Compute the count**

In `dashboard.py`, inside the `topbar` dict (`app` builder `full_update`, `dashboard.py:148-160`), add after the `"scheduled": ...` entry:

```python
                "dirty": sum(1 for card in cards if card.git.dirty_count),
```

- [ ] **Step 4: Run the test to confirm it passes**

Run: `uv run pytest tests/test_dashboard.py -k topbar -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: PASS (additive key; `test_static_js.py` topbar fixtures don't assert key-exhaustiveness).

- [ ] **Step 6: Commit**

```bash
git add src/bridge/dashboard.py tests/test_dashboard.py
git commit -m "Expose dirty-tree count in the topbar envelope"
```

---

### Task 5: Overview at-a-glance command strip

**Files:**
- Modify: `src/bridge/templates/overview.html` — add the strip markup at the top of `{% block content %}` (`overview.html:37`)
- Modify: `src/bridge/static/app.css` — add `.overview-command-strip` component CSS (near the other `.overview-*` rules, ~`app.css:958`)
- Test: `tests/test_overview.py`

**Interfaces:**
- Consumes: `model.totals.running`, `model.totals.queued`, `model.totals.dirty` (Task 4), `model.totals.scheduled`, `model.totals.projects`, and `model.attention_total`.
- Produces: the six-cell command band. Server-rendered only (see Out of scope); refreshed on full page load.

**Cell taxonomy** (decision — the first thing to tinker on visually once rendered): six already-computed, distinct system counts. Color is used sparingly, per spec §5.4: only two cells carry status color — `Running` (forest, live) and `Needs attention` (terracotta, the page's thesis roll-up that matches the masthead subtitle and the Projects "Needs attention" pill). `Queued`, `Dirty trees`, `Scheduled`, and `Projects` are neutral informational counts (spec §5.4 maps Queued and Stale to muted, not attention). Each colored cell lights up only when its value is non-zero.

| Cell label | Value | Color when non-zero |
|---|---|---|
| Running | `model.totals.running` | forest (`.is-live`) |
| Needs attention | `model.attention_total` | terracotta (`.is-hot`) |
| Queued | `model.totals.queued` | neutral |
| Dirty trees | `model.totals.dirty` | neutral |
| Scheduled | `model.totals.scheduled` | neutral |
| Projects | `model.totals.projects` | neutral |

- [ ] **Step 1: Write the failing test**

In `tests/test_overview.py`, add a render test that drives the route with a known state and asserts the strip is present with the right numerals. Follow the file's existing `TestClient(create_app(...))` pattern (see the imports at `test_overview.py:1-15`). Assert the rendered HTML contains the strip container and each label, e.g.:

```python
def test_overview_renders_command_strip_with_counts(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    # ... seed one running session + one dirty project via the same fixture
    #     helpers the other tests in this file use (probe_fn/agents_fn) ...
    client = TestClient(create_app(cfg))
    html = client.get("/").text
    assert 'class="overview-command-strip"' in html
    assert "Needs attention" in html
    assert "Dirty trees" in html
    assert "Running" in html
```

(Reuse the seeding style from `test_attention_ladder_orders_kinds_and_pins_correct_hrefs` for the running/dirty state; the assertion is presence of the strip + labels, so it stays robust to exact counts.)

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_overview.py::test_overview_renders_command_strip_with_counts -v`
Expected: FAIL — `'class="overview-command-strip"'` not found.

- [ ] **Step 3: Add the strip markup**

At the very top of `{% block content %}` in `overview.html` (immediately after `overview.html:37`, before `<section class="overview-attention" ...>`), insert:

```jinja
<div class="overview-command-strip" aria-label="System at a glance">
  <div class="command-cell{% if model.totals.running %} is-live{% endif %}">
    <span class="command-cell__num">{{ model.totals.running }}</span>
    <span class="command-cell__label">Running</span>
  </div>
  <div class="command-cell{% if model.attention_total %} is-hot{% endif %}">
    <span class="command-cell__num">{{ model.attention_total }}</span>
    <span class="command-cell__label">Needs attention</span>
  </div>
  <div class="command-cell">
    <span class="command-cell__num">{{ model.totals.queued }}</span>
    <span class="command-cell__label">Queued</span>
  </div>
  <div class="command-cell">
    <span class="command-cell__num">{{ model.totals.dirty }}</span>
    <span class="command-cell__label">Dirty trees</span>
  </div>
  <div class="command-cell">
    <span class="command-cell__num">{{ model.totals.scheduled }}</span>
    <span class="command-cell__label">Scheduled</span>
  </div>
  <div class="command-cell">
    <span class="command-cell__num">{{ model.totals.projects }}</span>
    <span class="command-cell__label">Projects</span>
  </div>
</div>
```

- [ ] **Step 4: Add the strip CSS**

Near the `.overview-*` rules (~`app.css:958`), add:

```css
.overview-command-strip {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  margin: var(--space-6) 0 var(--space-8);
  border: 1px solid var(--rule);
  border-radius: var(--radius-md);
  background: var(--surface);
  overflow: hidden;
}
.command-cell {
  padding: var(--space-4) var(--space-4);
  border-right: 1px solid var(--rule);
}
.command-cell:last-child { border-right: 0; }
.command-cell__num {
  display: block;
  font: 600 1.875rem/1 var(--font-display);
  letter-spacing: -.01em;
  font-variant-numeric: tabular-nums lining-nums;
  color: var(--text);
}
.command-cell.is-live .command-cell__num { color: var(--status-running); }
.command-cell.is-hot .command-cell__num { color: var(--status-attention); }
.command-cell__label {
  display: block;
  margin-top: var(--space-2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--text-secondary);
}
```

Add a narrow-width reflow to the existing `@media (max-width: 767px)` block (`app.css:1381`):

```css
  .overview-command-strip { grid-template-columns: repeat(3, 1fr); }
  .command-cell:nth-child(3n) { border-right: 0; }
  .command-cell:nth-child(-n+3) { border-bottom: 1px solid var(--rule); }
```

- [ ] **Step 5: Run the test + full suite**

Run: `uv run pytest tests/test_overview.py -v && uv run pytest`
Expected: PASS.

- [ ] **Step 6: Render + visual check**

Restart the panel; headless-screenshot `/` and Read the PNG. Confirm the six-cell ruled band with Fraunces numerals and mono uppercase labels: forest "Running" and terracotta "Needs attention" numerals when non-zero; Queued/Dirty trees/Scheduled/Projects neutral. Compare to the mockup strip.

- [ ] **Step 7: Commit**

```bash
git add src/bridge/templates/overview.html src/bridge/static/app.css tests/test_overview.py
git commit -m "Add the Overview at-a-glance command strip"
```

---

### Task 6: Status-top-border attention cards + hero enrichment + numbered sections

**Files:**
- Modify: `src/bridge/overview.py:197-262` (attention item `meta`: add `path`)
- Modify: `src/bridge/templates/overview.html:38-106` (section numerals + italic hint, status classes, three-cue labels, hero detail rows + ghost action + path footer)
- Modify: `src/bridge/static/app.css` — `.attention-primary`/`.attention-secondary` (980-1057), `.overview-attention h2, .overview-panel h2` (961-962), `.overview-section-head` (963-972); add status-border, slug, detail-row, path, and section-label rules
- Test: `tests/test_overview.py` + full suite + visual

**Interfaces:**
- Consumes: `model.attention` items (each `.kind` ∈ handoff|running|stale|schedule_failure); `Card.path`/`Card.git` (in overview.py); the serif hero title (Task 3); status semantics (Task 2).
- Produces: `AttentionItem.meta["path"]` (per-card kinds). The Almanac attention surface: colored **top-border** by status with three redundant cues (text label + mono slug + color), an enriched hero (Last activity, Waiting on, ghost action, path footer), and small-caps numbered section labels. No status is color-only.

**Status mapping** (kind → color class / text label / mono slug / waiting-on label). `waiting_on` is a true, kind-derived one-liner rendered in the template — NOT fabricated telemetry (`LiveSession.status` is an open vocabulary per `models.py:66`, so no per-session "waiting" reason is invented; the running hero omits the row):

| `kind` | card class | text label (pill) | mono slug | `waiting_on` |
|---|---|---|---|---|
| handoff | `st-attention` | Ready to continue | `queued` | Your input to continue |
| running | `st-run` | Working now | `running` | — (unknown; omit) |
| stale | `st-review` | Needs review | `stale` | A commit or cleanup |
| schedule_failure | `st-risk` | Failed | `failed` | Review of the failed run |

(`handoff` keeps the existing template's accurate "Ready to continue" — a queued handoff is ready *for* you — colored terracotta as the primary attention, not "Needs input".)

- [ ] **Step 1: Project `path` into the attention meta (TDD)**

In `tests/test_overview.py`, extend `test_attention_ladder_orders_kinds_and_pins_correct_hrefs` (the hero is the handoff item) with:

```python
    assert model.attention[0].meta["path"] == "/p/handoff"  # hero renders a path footer
```

Run: `uv run pytest tests/test_overview.py -k attention -v`
Expected: FAIL — `KeyError: 'path'`.

In `overview.py`, add `"path": card.path,` to the `meta=` dict of each per-card attention item: the handoff branch (`overview.py:222-229`), the running branch (`overview.py:240-245`), and the stale branch (`overview.py:256-260`). Leave `_schedule_failures` (`overview.py:302-309`) as-is — those may have no card; the template guards the footer on `primary.meta.path`. Do not add a backend `waiting_on`: it is a template-side kind lookup (Step 3).

Run the `-k attention` test → PASS, then `uv run pytest` → PASS.

- [ ] **Step 2: Number the sections + add the italic hint**

In `overview.html`, edit the attention section head (`overview.html:39-48`) so the `<h2>` is preceded by a mono numeral and followed by an italic serif hint. Replace the `<div>` holding the h2+p with:

```jinja
    <div>
      <p class="overview-section-head__num">01</p>
      <h2 id="overview-attention-title">Needs attention</h2>
      <p class="overview-section-head__hint">Running work, queued handoffs, failures &amp; stale projects.</p>
    </div>
```

Do the same for the "Recent projects" head (`overview.html:110-113`) with `02` and hint "Continue where you last left off." (referenced again in Task 7).

- [ ] **Step 3: Map each attention item to a status class + three cues**

In `overview.html`, at the top of the `{% if model.attention %}` branch (`overview.html:52`), add the mapping (fourth tuple element = the `waiting_on` label; empty string = omit):

```jinja
{% set status_map = {
  "handoff": ("st-attention", "Ready to continue", "queued", "Your input to continue"),
  "running": ("st-run", "Working now", "running", ""),
  "stale": ("st-review", "Needs review", "stale", "A commit or cleanup"),
  "schedule_failure": ("st-risk", "Failed", "failed", "Review of the failed run"),
} %}
```

For the primary: `{% set pcls, plabel, pslug, wait = status_map.get(primary.kind, ("st-attention", "Ready to continue", primary.kind, "")) %}`. Render the article as `class="attention-primary attention-primary--{{ pcls }}"`, the pill as `<span class="pill pill--{{ pcls }}">{{ plabel }}</span>`, and append the mono slug to the kicker line (`… · <span class="attention-kicker__slug">{{ pslug }}</span>`). Apply the same lookup to each secondary item (`overview.html:89-100`) for its class + pill label (secondaries need only class/label, not the detail rows).

- [ ] **Step 4: Enrich the hero — detail rows, ghost action, path footer**

Replace the hero's inline `.attention-primary__meta` span (the branch/dirty/created strip, `overview.html:79-83`) with a detail list, so that info reads as labeled "Last activity" instead of a cramped meta line. Every field is guarded — an absent value drops its row (progressive enhancement, spec §11):

```jinja
        <dl class="attention-detail">
          {% if primary.meta.created_at %}
          <div class="attention-detail__row">
            <dt>Last activity</dt>
            <dd>{{ primary.meta.created_at | ago_epoch }} ago{% if primary.meta.branch %} · {{ primary.meta.branch }}{% endif %}{% if primary.meta.dirty_count %} · {{ primary.meta.dirty_count }} dirty{% endif %}</dd>
          </div>
          {% endif %}
          {% if wait %}
          <div class="attention-detail__row">
            <dt>Waiting on</dt><dd>{{ wait }}</dd>
          </div>
          {% endif %}
        </dl>
```

Give every hero a primary + a **non-redundant** ghost action. Replace the handoff-only secondary at `overview.html:78`:

```jinja
          {% if primary.kind == "handoff" %}
            <a class="btn" href="{{ primary.primary_action.href }}">Review handoff</a>
          {% elif primary.project_id %}
            <a class="btn" href="/project/{{ primary.project_id }}?tab=current">View session</a>
          {% endif %}
```

(The ghost points at the project's *current/session* tab — a genuinely distinct view from the primary "Open project" destination, so it is never a duplicate link. `schedule_failure` items with no `project_id` get no ghost, which is correct — one action is enough there.)

Add the path footer at the end of the article (after the actions row, before `</article>`):

```jinja
        {% if primary.meta.path %}<p class="attention-primary__path">{{ primary.meta.path }}</p>{% endif %}
```

- [ ] **Step 5: Restyle the cards, section labels, and hero details**

Replace the left-border treatment on `.attention-primary` (`app.css:980-987`) and remove the `--stale/--schedule_failure` left-border overrides (`app.css:988-989`), swapping to a top-border by status class:

```css
.attention-primary {
  min-width: 0;
  padding: 1.1875rem 1.25rem 1.125rem;
  background: var(--surface);
  border: 1px solid var(--rule);
  border-top: 4px solid var(--rule);
  border-radius: 11px;
}
.attention-primary--st-run { border-top-color: var(--status-running); }
.attention-primary--st-attention { border-top-color: var(--status-attention); }
.attention-primary--st-review { border-top-color: var(--review); }
.attention-primary--st-risk { border-top-color: var(--risk); }
.attention-secondary { border-top: 4px solid var(--rule); }
.attention-secondary--st-run { border-top-color: var(--status-running); }
.attention-secondary--st-attention { border-top-color: var(--status-attention); }
.attention-secondary--st-review { border-top-color: var(--review); }
.attention-secondary--st-risk { border-top-color: var(--risk); }
```

Update the `.attention-kicker` color overrides (`app.css:1000-1001`, `1034`) so the kicker/`__labels` accent reads `var(--work)` uniformly (the removed `--stale`/`--schedule_failure` selectors no longer exist), and add the pill + slug + detail + section-label rules:

```css
.pill--st-run { color: var(--pill-run-fg); background: var(--pill-run-bg); }
.pill--st-attention { color: var(--pill-work-fg); background: var(--pill-work-bg); }
.pill--st-review { color: var(--pill-review-fg); background: var(--pill-review-bg); }
.pill--st-risk { color: var(--pill-risk-fg); background: var(--pill-risk-bg); }
.attention-kicker__slug { font-family: var(--font-mono); text-transform: none; letter-spacing: 0; color: var(--text-secondary); }

.attention-detail { margin: var(--space-4) 0 0; }
.attention-detail__row {
  display: flex;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-2) 0;
  border-top: 1px solid var(--rule);
  font-size: var(--text-sm);
}
.attention-detail__row dt {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--text-secondary);
}
.attention-detail__row dd { margin: 0; text-align: right; color: var(--text); overflow-wrap: anywhere; }
.attention-primary__path {
  margin: var(--space-3) 0 0;
  padding-top: var(--space-3);
  border-top: 1px dotted var(--rule);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
```

Restyle the section headings (`.overview-attention h2, .overview-panel h2`, `app.css:961-962`) into the small-caps Almanac label — Atkinson (UI face), NOT serif, since they sit well under the 19px display threshold:

```css
.overview-attention h2,
.overview-panel h2 {
  font: 700 var(--text-sm)/1.3 var(--font-sans);
  letter-spacing: .12em;
  text-transform: uppercase;
}
```

- [ ] **Step 6: Verify + render**

Run: `uv run pytest`
Expected: PASS. (No `test_overview.py` test asserts the old pill class names or label strings — verified during planning — so the class/label rename is safe. If a suite elsewhere pins the string "Ready to continue"/"Working now", it still holds; the only changed label is the schedule-failure "Failed", previously "Needs review" — grep `git grep -n "Needs review" tests` before committing and update any test that pinned it on a schedule-failure row.)
Restart the panel; headless-screenshot `/` (light) and Read the PNG. Confirm: `01`/`02` mono numerals, small-caps section labels, top-border status colors, "Ready to continue / Working now / Needs review / Failed" each beside a mono slug, and the enriched hero (Last activity + Waiting on rows, ghost action, dotted path footer).

- [ ] **Step 7: Commit**

```bash
git add src/bridge/overview.py src/bridge/templates/overview.html src/bridge/static/app.css tests/test_overview.py
git commit -m "Enrich Overview attention cards: status top-borders, three cues, hero detail"
```

---

### Task 7: Editorial recent-project rows

**Files:**
- Modify: `src/bridge/static/app.css:1065-1068` (`.overview-recent .project-row` overrides)
- Test: full suite green + visual

**Interfaces:**
- Consumes: `project_summary_row` macro output (unchanged markup) inside `.overview-recent`. The macro renders `<a class="btn" ...>Open project</a>` (`_components.html:69`) — **not** "Open →".
- Produces: hairline-ruled editorial rows — serif name (bumped to 20px, ≥19px display threshold) + mono path, branch/description, and an `Open project →` action. **CSS-only**: the accessible label stays "Open project" (an arrow alone is a poorer accessible name); the `→` is a decorative `::after`, so no template/markup change and no macro fork.

- [ ] **Step 1: Restyle the recent rows**

Replace the `.overview-recent` overrides (`app.css:1065-1068`) with the editorial treatment — hairline top+bottom rules, roomier rhythm, serif 20px name (scoped here, since the Projects copy of this macro stays 16px Atkinson per the spec's ≥19px serif rule), and a quiet terracotta action with a `→`:

```css
.overview-recent .projects-list { border-top: 1px solid var(--rule); }
.overview-recent .projects-list__item {
  display: block;
  padding: 0;
  border-bottom: 1px solid var(--rule);
}
.overview-recent .project-row {
  min-height: 3.75rem;
  padding: var(--space-3) 0;
  grid-template-columns: minmax(9rem, 1fr) minmax(10rem, 1.35fr) auto;
  gap: var(--space-4);
  align-items: center;
}
.overview-recent .project-row__name {
  font-family: var(--font-display);
  font-weight: 600;
  font-size: var(--text-title);
}
.overview-recent .project-row__action .btn {
  min-height: auto;
  padding: var(--space-1) var(--space-2);
  color: var(--work);
  background: transparent;
  border-color: transparent;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  letter-spacing: .04em;
}
.overview-recent .project-row__action .btn::after { content: " →"; }
.overview-recent .project-row__action .btn:hover {
  background: var(--surface-raised);
  border-color: transparent;
}
```

- [ ] **Step 2: Verify + render**

Run: `uv run pytest`
Expected: PASS (CSS-only; no markup/behavior change — the button's accessible name stays "Open project").
Restart the panel; headless-screenshot `/` (light) and Read the PNG. Confirm the recent list reads as editorial rows: serif 20px names, mono paths, hairline rules only (no full grid), quiet terracotta `Open project →`.

- [ ] **Step 3: Commit**

```bash
git add src/bridge/static/app.css
git commit -m "Give Overview recent projects editorial list rows"
```

---

### Task 8: Both-theme verification + visual review gate

**Files:** none (verification only).

**Interfaces:**
- Consumes: the full Overview build (Tasks 1–7).
- Produces: passing suite + light & dark renders for Mit to review and tinker (he calibrates on rendered output — memory `user-iterates-visually-on-rendered-ui`).

- [ ] **Step 1: Full suite**

Run: `uv run pytest`
Expected: PASS — contrast (both themes, incl. the six new status pairs), fonts, overview, dashboard, static-js, and every other suite green.

- [ ] **Step 2: Render both themes at desktop + narrow width**

Restart the panel on `127.0.0.1:8787`. Capture four PNGs into the session scratchpad and Read each:
- Overview, light, 1440px wide
- Overview, dark, 1440px wide (set `bridge.appearance=dark` via the Claude-in-Chrome extension's `javascript_tool` on `localStorage`, then screenshot; if the extension is disconnected, capture dark via headless Chrome with the OS in dark appearance)
- Overview, light, 390px wide (confirm the strip reflows to 3×2 and the grids collapse)
- Overview, dark, 390px wide

- [ ] **Step 3: Self-check against the spec's success criteria**

Confirm against spec §12: reads as unmistakably Bridge (passes the `anti-ai-generic-look` smell test); the strip states the whole system's status at a glance; full WCAG AA both themes; the token/type/component system is reusable by later pages. Fix any visual regression inline (small CSS nudges), re-run `uv run pytest`, and re-render.

- [ ] **Step 4: Hand the renders to Mit for visual review**

Present the light + dark renders and note the explicit tinker points: the command-strip cell taxonomy (labels/order), masthead title size, and the double-rule weight. Iterate from his feedback.

---

## Self-Review

**1. Spec coverage** (spec §-by-§ → task):
- §4 Typography (Fraunces, **serif at ≥19px display sizes only**, tabular numerals) → Task 1 (bundle), Task 3 (masthead + card titles), Task 5 (strip numerals), Task 6 (hero title 22px; section labels stay Atkinson small-caps), Task 7 (recent-row name 20px). Sub-19px slots (mini-card h3, dense Projects names) deliberately stay Atkinson.
- §5 Color (warm primitives both themes, accent shift at primitive layer, status roles, status taxonomy with three cues) → Tasks 2, 5, 6.
- §6 Layout: masthead + double rule → Task 3; command strip → Task 5; `01`/`02` numbered sections + hero/mini attention cards → Task 6; enriched hero (Last activity / Waiting on / ghost action / path footer, §6.3) → Task 6; editorial recent rows → Task 7. ("Up next / usage" preserved, re-tinted by cascade — no task needed.)
- §7 Components: card top-border status → Task 6; command-strip cell → Task 5; buttons (terracotta primary) → cascade from Task 2 + row buttons in Task 7; status tag → Task 6; list rows → Task 7; real SVG icons → unchanged (constraint honored, no emoji introduced).
- §8 Motion: unchanged; existing reduced-motion rules and transform/opacity-only transitions untouched (constraint).
- §9 Accessibility contract: contrast → Task 2 (+ extended suite); dark accents desaturated/lightened → Task 2; status never color-only → Task 6 (label + slug + color, three cues); decorative hairlines WCAG-exempt while control boundaries keep 3:1 via `--field-line` (see Global Constraints); focus/targets/reduced-motion → inherited, not regressed.
- §10 Rollout (Overview first) → Tasks 1–8; the six counts + dirty/scheduled → Tasks 4–5; hero "Waiting on" → Task 6 as a **kind-derived** label (guarded, degrades to omitted), with `path` projected in `overview.py` (Task 6 Step 1) — no fabricated telemetry.
- §11 Risks: gold-on-cream AA fail caught and fixed (darker `--p-review #7e5a10`); **two accent/soft pairs corrected after the contrast recompute** (light `--p-work` → #a23c17, dark `--p-work-soft` → #301f16 — the mockup values were 4.48:1 / 4.34:1, below 4.5); terracotta-as-text verified in the pairs; dark terracotta button text keeps `--btn-primary-fg: var(--surface)` via cascade, confirmed in Task 8's dark render (nudge `--p-work` if the label is under AA at button size).

**2. Placeholder scan:** no "TBD"/"handle edge cases"/vague steps — every code step carries the actual CSS/Jinja/Python. The one unavoidable external action (fetching the Fraunces binary) is fully specified: exact filenames, license file, provenance format, and the `@font-face`/token/preload wiring around it.

**3. Type consistency:** primitive names are **retained** (re-valued), so `test_contrast.py`'s references stay valid; new primitives `--p-run`/`--p-run-soft`/`--p-review`/`--p-review-soft` are defined in Task 2 before any later task consumes them. Status classes are one vocabulary throughout: `st-run` / `st-attention` / `st-review` / `st-risk`, used identically on `.attention-primary--*`, `.attention-secondary--*`, and `.pill--*` (Task 6). `--font-display` is defined in Task 1 and consumed in Tasks 3/5/6/7. `model.totals.dirty` is produced in Task 4 and consumed in Task 5; `meta["path"]` is produced in Task 6 Step 1 and consumed in the same task's hero footer.

**4. Codex review (one pass, folded):** a single Codex review ran against the plan + spec + codebase. Folded: the two failing contrast pairs (verified by recompute); the `--rule` boundary-contrast wording (decorative hairlines are WCAG-exempt — kept the existing shipped behavior rather than minting boundary tokens); the shared-macro action label (`Open project`, arrow via CSS `::after`, not a fabricated "Open →"); the missing hero enrichment + `path` projection (Task 6); the "Queued" strip cell recolored to neutral per spec §5.4; the serif-threshold corrections (mini-card/section/row typography); the `theme-color` meta update (Task 2); and a Fraunces provenance assertion (Task 1). Not folded as written: a separate boundary-token/test for decorative rules (over-engineering — the hairlines are exempt and already ship lighter than 3:1 by design).
