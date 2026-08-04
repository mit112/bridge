# Bridge Almanac — Projects screen (`/projects`)

**Date:** 2026-08-03
**Status:** Draft for review
**Branch:** `feat/bridge-product-redesign` (HEAD `872b237`, 979 passing)
**Parent spec:** `docs/superpowers/specs/2026-08-03-bridge-almanac-visual-redesign-design.md`
**Predecessor pass:** `docs/superpowers/plans/2026-08-03-bridge-almanac-overview.md` + its ledger
`.superpowers/sdd/2026-08-03-bridge-almanac-overview/progress.md`

---

## 1. Problem

The Almanac system's *global* layer already reaches `/projects`: warm cream/espresso palette,
Fraunces masthead, italic serif lede, double rule, terracotta accent, and the fixed sidebar rail
(`872b237`) all cascade for free. Nothing structural does. The page is still the pre-redesign
layout, and it has measurable defects — all figures below are **measured in the running app at
`data-theme="dark"`**, not estimated:

| Defect | Measurement |
|---|---|
| **The right edge is ragged.** The `<summary>` reads `Actions for {{ row.name }}`, so the `auto` action column resizes per row. | `Open project` left edge spans **1070→1192px — a 122px jitter** across 36 rows. Summary widths **89→211px**. |
| **Rows are mostly air.** | 81–82px per row → **2930px of list** for 36 projects at 1440. |
| **Names never got the Almanac treatment.** | `.project-row__name` computes to **16px / 700 / Atkinson Hyperlegible Next**. The parent spec §4 puts project names in Fraunces. |
| **Ragged row heights off-desktop.** The 768–1250 band was never designed. | 1100px: rows **81→108px**. 768px: rows **81→155px**, list **3072px**. |
| **The status column is a low-information void.** | 36 tinted pills, 28 of them `stale`/`recent`, floating mid-row in a wide empty band. |

## 2. Goals / non-goals

**Goals**
- Make `/projects` read as the same publication as the Overview — an *index* in the almanac,
  not a different product.
- Make 36 projects scannable: less scroll, one alignment axis, the attention band visible at a glance.
- Fix the ragged action column and the ragged tablet band, both measured above.
- Keep WCAG 2.2 AA in both themes; keep `tests/test_contrast.py` green.

**Non-goals**
- No change to routes, IA, the read model (`bridge/projects_view.py`), or `ProjectSummary`.
- **No change to `project_summary_row`'s markup** (see §7).
- No change to `projects.js`'s no-reload contract, its `data-project-*` hooks, or its filter predicate.
- Not fixing the eight uncoloured schedule pills or the dead `.scheduled__status` rules — both are
  pre-existing, both surface on `/schedule` and the Overview's "Up next", neither on `/projects`.

## 3. Decisions taken, and why

These were put to Mit as a batched question; he was away, so they are taken on the reasoning below
and every one is **reversible at his tinker pass**. §11 lists them as open.

### 3.1 Flat ledger, not grouped sections — because pinning forbids the partition

The tempting move is Overview-style numbered sections: `01 Needs attention` / `02 Everything else`.
**Rejected on the code, not on cost.** `bridge/cards.py:sort_key` returns:

```python
return (0 if card.pinned else 1, rank, live_rank, -(ended or 0), card.name.lower())
```

Pin is the **first** term — it outranks `rank`. Its docstring is explicit:

> *"Pin outranks everything, a queued handoff included. … a pin is the one thing the user said
> outright, and an inference must not overrule an instruction."*

So a pinned *idle* project legitimately sorts above the queued/running/stale band. Partitioning the
list by inferred attention would move that row **down** the page — inference overruling instruction,
exactly what the model refuses to do. A "Pinned" third section only relocates the problem and adds a
head that is empty in the common case.

**Instead: the sort already groups — make it legible.** Rank order already emits
queued → running → stale → recent as a clean run in the normal case. A status-colored left edge (§4.2)
makes that run *visible as a block* without asserting a partition that pinning can violate, and a
pinned row reading out of band is then self-explanatory because it carries a pin marker (§4.4).

The filter chips already provide the interactive grouping, so nothing is lost.

### 3.2 Status = colored left edge + uncoloured small-caps mono word

Replaces the tinted pill **on this page only**. Rationale: 36 tinted chips is a lot of chrome for a
field that is `stale` or `recent` 28 times out of 36, and the pill's rounded fill fights the ruled
ledger idiom. Two redundant cues remain — the edge **and** the word — so WCAG 1.4.1 holds.

**This needs no new contrast gate.** `tests/test_contrast.py` already carries all six pairs the
uncoloured word requires, at the 4.5:1 text threshold:

```
("--p-run",    "--p-surface", 4.5)   ("--p-run",    "--p-canvas", 4.5)
("--p-review", "--p-surface", 4.5)   ("--p-review", "--p-canvas", 4.5)
("--p-work",   "--p-surface", 4.5)   ("--p-work",   "--p-canvas", 4.5)
```

Colour mapping is inherited verbatim from `7a61923` and **is not relitigated**:

| `data-project-state` | `status_word` | Edge + word colour | Token |
|---|---|---|---|
| `running` | running | forest green | `--run` |
| `stale` | stale | gold | `--review` |
| `queued` | queued | terracotta | `--work` |
| `idle` | recent | none (transparent edge) | — |

The `idle` edge is deliberately **transparent, not `--rule`**: a hairline in the rule colour on every
quiet row would re-add the visual noise this change removes, and "no edge" is itself the signal.

This also resolves the standing finding that `--p-work` (terracotta `#a23c17`) and `--p-risk`
(deep rust `#99301a`) are adjacent hues — Projects simply stops using `--p-risk`, so the two never
appear side by side here. Widening the hue gap stays available but is no longer forced by this page.

### 3.3 Fixed-width action column

The measured 122px jitter is caused by one string: `Actions for {{ row.name }}` in the `<summary>`.
The fix is to move the project name out of the visible summary text and into an `aria-label`, then
give the action column a fixed width. The disclosure keeps its accessible name (the name is what a
screen reader announces either way); only the *rendered* text becomes the constant `Actions`.

This is a `projects.html` edit, **not** a macro edit — the `<details>` lives in the page template.

### 3.4 Two-line rows, ~64px

Line 1: serif name + status word + metadata. Line 2: mono path. Hairline rule between rows.
Target ≈2300px for 36 rows (down from 2930), without cramping — serif names stay at
`--text-title` (19px+), honouring the spec's "serif at 19px and up only" floor.

## 4. Row anatomy

The macro emits exactly these hooks, and all of them are already present:

```
li.projects-list__item[data-project-state][data-project-name][data-project-path]
  div.project-row
    span.project-row__identity  > span.project-row__name , code.project-row__path
    span.project-row__activity  > .pill , .project-row__branch , .project-row__dirty , .project-row__session
    span.project-row__action    > a.btn "Open project"
  details.projects-list__actions > summary , button[data-project-pin] , button[data-project-hide] , span
```

### 4.1 Grid

`.projects-index .project-row` keeps its existing three grid children and its existing internal
two-line shape (`__identity` already stacks name over path as a nested grid) — the status word must
land on a shared axis to scan, so `__activity` stays its own column rather than flowing inline:

```
┌─────────────────────────────────────┬──────────────┬───────────┐
│ Fraunces name        STATUS · meta  │              │           │
│ mono/path/to/project                │   Open →     │ ▸ Actions │
└─────────────────────────────────────┴──────────────┴───────────┘
  minmax(0, 1fr)                        <fixed>        <fixed>
```

Density therefore comes from tightening padding and line-height (`.project-row` is currently
`padding: var(--space-4) 0`, giving the measured 81px), **not** from restructuring — so **no markup
moves**.

### 4.2 Status edge

Applied to `.projects-index .projects-list__item` via the existing `[data-project-state]` attribute
— per §3.2's mapping. No new class, no macro change. Implemented as `box-shadow: inset 3px 0 0`
rather than `border-left` so it does not participate in box sizing and cannot shift the grid's
column tracks between states.

### 4.3 Typography

| Slot | Treatment |
|---|---|
| `.project-row__name` | `var(--font-display)` 600, `var(--text-title)` — matches `.overview-recent .project-row__name` exactly, so the two pages agree |
| `.project-row__path` | mono, `--text-xs`, `--text-secondary`, ellipsised (unchanged idiom) |
| `.pill` (status word) | background/border stripped; mono, uppercase, `--text-xs`, `letter-spacing: .06em`, colour per state |
| `.project-row__branch` / `__dirty` | mono `--text-xs`, `--text-secondary` |
| `.project-row__session` | Atkinson `--text-sm`, `--text-secondary`, single-line ellipsis |
| `.project-row__action .btn` | mirrors `.overview-recent`'s ghost: mono `--text-xs`, terracotta, transparent, `→` suffix |

### 4.4 Pinned marker

`ProjectSummary.pinned` exists but the macro never renders it, and the `<li>` carries no pinned hook
— so today **a pinned project is visually indistinguishable** even though pinning is the strongest
term in the sort. Since §3.1 leans on pin being self-explanatory, it has to be visible.

Resolved CSS-only with `:has()`, reading the Pin button's `aria-pressed` (which `projects.js`
already keeps as the single source of truth):

```css
.projects-list__item:has([data-project-pin][aria-pressed="true"]) .project-row__name::after { … }
```

`:has()` is Baseline-supported; where it is not, the row simply lacks the marker — no layout break.
The marker is **decorative only**: the Pin button's own `aria-pressed` is already the programmatic
signal, so the marker takes `content` that is not announced, and no accessible information depends
on it.

## 5. Controls bar

Restyled into the ledger idiom, no markup change beyond §3.3:

- **Search** — label stays visible (placeholders are not labels); field becomes a bottom-ruled
  input rather than a boxed one, matching the almanac's ruled-form feel. Keeps `--control-min`.
- **Filter chips** — mono, uppercase, `--text-xs`; counts in tabular figures. The pressed chip keeps
  a solid terracotta fill (`aria-pressed` remains the state). Five chips stay a toggle group, **not**
  radios — `projects.js` reads `aria-pressed` and changing that breaks the filter.
- **Result count** — mono, `--text-secondary`; stays `role="status" aria-live="polite"`.

## 6. Index header band

A column-header band above the list — small-caps mono labels on the same axes as the rows
(`PROJECT` / `STATUS & ACTIVITY` / blank). This supplies the "almanac ledger" structure that §3.1
declined to get from section heads, at zero ordering risk.

Per `ux-table-scannable` ("headers carry weight, not a ruled line"; "increase padding before adding
borders"), the band is distinguished by **weight, letter-spacing and small caps — not by a rule of its
own**. The single hairline below it is the list's existing top border, not an added one, so the band
adds no net rule to the page.

It is **presentational**, not a table header: the list is a `<ul>`, not a `<table>`, so the band is
`aria-hidden="true"` — announcing "Project, Status and activity" before a list that is not a grid
would be a false structural promise.

## 7. Shared-macro containment (the biggest trap in this pass)

`project_summary_row` (`_components.html:54`) is consumed by **both** `/projects` (`projects.html:41`)
and the Overview's Recent projects (`overview.html:191`). The Overview pass restyled it by scoping
everything under `.overview-recent` and leaving the markup alone — which is why `/projects` still has
16px Atkinson names today.

**This pass does the same, in the other direction: the macro's markup is not touched.** Chosen over
forking/parameterising it because:

1. Every hook the design needs already exists — including `[data-project-state]` on the `<li>`,
   which is a first-class status hook requiring no new markup at all.
2. `tests/test_projects_route.py:211` asserts the macro's child order
   (`identity < activity < action`) in the raw HTML. Reordering is off the table anyway.
3. Zero blast radius: Overview's rows cannot change if the markup does not.

### 7.1 ⚠️ `.projects-list` is NOT a Projects-only scope

The obvious scope is wrong, and this is the single highest-risk fact in the pass. `overview.html:189`
renders:

```jinja
<ul class="projects-list">
  {% for row in model.recent %}<li class="projects-list__item">{{ project_summary_row(row) }}</li>{% endfor %}
```

So **the Overview's Recent list carries the same `projects-list` / `projects-list__item` class names**,
not merely the same macro. The Overview isolates itself by *overriding* under `.overview-recent`
(`app.css` ~1292–1320), which works only because those overrides currently sit later in the sheet than
the base `.projects-list*` rules.

New Projects rules would land in the Projects block (~1549+) — **after** `.overview-recent`. At equal
specificity (`.projects-index .project-row` vs `.overview-recent .project-row`, both 0,2,0) source
order decides, so an unscoped `.projects-list` rule would win and silently restyle the Overview.

**Therefore: `projects.html`'s `<ul>` gains a dedicated `projects-index` class, and every new rule in
this pass is scoped under it.** The Overview's `<ul>` does not have it, so the two selectors can never
co-match and source order never arbitrates. A purpose-named class is used rather than the existing
`[data-projects-list]` attribute so the CSS does not couple itself to a JS hook name.

`.projects-controls`, `.projects-search`, `.projects-filters`, `.projects-count` and
`.hidden-projects-section` are already Projects-only and need no extra scope.

⚠️ `.overview-recent` scoping held last pass; it is **not** assumed to hold now. The Overview's Recent
projects list is **render-verified** before this pass is called done (§10), not reasoned about.

### 7.2 Markup edits in scope

Three, all in `projects.html` (the page template), none in the macro:
1. `projects-index` class on the `<ul>` (§7.1).
2. The `<summary>` text becomes the constant `Actions`, with the project name moved to `aria-label` (§3.3).
3. The `aria-hidden` column-header band above the list (§6).

`_components.html` is **not edited by this pass**. A diff touching it means the containment strategy
has been abandoned and the Overview must be re-verified from scratch.

## 8. Density & responsive

Render checkpoints: **1440, 1280, 1100, 768, 390.** The 1024–1250 band is included on purpose — the
Overview pass shipped a wrap bug that existed only there because nobody rendered it, and `/projects`
already measures 81→108px ragged at 1100 and 81→155px at 768.

- **≥1024** — three tracks as §4.1 (identity | activity | fixed action).
- **768–1023** — two tracks (identity | activity); the action cell moves to a **second grid row**
  spanning both, right-aligned. Every row then has the same two-row structure, so heights equalise
  instead of ranging 81→155px. This band is measured after the change, not assumed.
- **≤767** — existing single-column stack, restyled; `Open →` and `Actions` sit inline on one line.

The acceptance measurement for every band ≥1024 is `actionSpread == 0` (see §10); for 768–1023 and
below it is that the row-height range collapses to ≤2px, matching the Overview pass's Fix 7 criterion.

## 9. Accessibility contract

A design-KB pass (305 cards) was run adversarially against §§3–8 *before* any CSS was written. It
independently produced four further arguments against the grouped-section option already rejected in
§3.1, and found the following defects in what this spec did plan. Each is now binding.

### 9.1 Must hold in this pass

1. **Contrast** — status word ≥4.5:1 in both themes: **already gated** by six existing PAIRS (§3.2).
   The status *edge* is a redundant graphic (the word always accompanies it), so
   `a11y-contrast-nontext`'s 3:1 bar for meaning-bearing graphics does not strictly bind — but the
   tokens clear 4.5:1 anyway, so it passes on either reading. No new PAIRS needed.
2. **Status never colour-only** — the word is always present (§3.2). Verified in grayscale.
3. **Small caps via `font-variant-caps: all-small-caps`, never `text-transform: uppercase`.**
   `ux-copy-clarity`: "Use sentence case … not 'ADD TO CART'." The source text is already lowercase
   (`status_word`), so CSS small caps is the sanctioned rendering and authored caps is not.
4. **12px floor.** `--text-xs` is exactly `.75rem`/12px. Nothing in this pass goes below it —
   `vis-typo-body-size`: "never go below 12px anywhere."
5. **Target size (WCAG 2.5.8).** The `<summary>` and the `Open →` link land in the same action
   column and would otherwise pack edge-to-edge. Each needs ≥24×24px **and** ≥24px unobstructed
   spacing. `sys-density-tradeoffs`: "Don't apply one density value uniformly to both a table's row
   height and its row-action buttons" — the row goes dense, the controls keep their hit area,
   padded independently of row height.
6. **The `<summary>` keeps the project name in its accessible name** via `aria-label` (§3.3).
   36 summaries all reading "Actions" is an unusable rotor. The visible text is constant; the
   accessible name is not.
7. **No fixed pixel action column.** `a11y-text-reflow`: "Don't pin fixed-width containers that
   overflow the small viewport"; `a11y-text-resize` covers 200% zoom, at which a 1440 viewport
   becomes 720 CSS px and must reflow. The track is `minmax()`/`ch`-based and collapses per §8.
8. **At most two separator channels.** `ux-table-density`: "Don't apply zebra striping AND heavy
   rule lines AND hover highlighting simultaneously." This pass ships the hairline row rule + the
   status edge, and therefore adds **no row-level hover tint** — hover feedback lives on the action
   link only.
9. **No truncation without an affordance.** `anti-truncation-no-affordance`: truncation-as-layout-fix
   silently destroys information, and long paths are currently ellipsised
   (`/Users/…/boardwatch/.claude/work…`). The path therefore **wraps instead of truncating**.
   Accepted trade: the ~4 longest paths make their rows taller, so §8's height-equalisation
   criterion applies to single-line-path rows only. Adding a `title` attribute would be the tidier
   fix but requires a macro edit, which §7 forbids.
10. Filter chips keep `aria-pressed`; counts are inside the button, so they are already part of the
    accessible name. Result count keeps `role="status" aria-live="polite"`.
11. Index header band is `aria-hidden` (§6); the pin marker is decorative (§4.4).
12. Focus ring must clear 3:1 against **both** the element and the page background — verified by
    measurement in both themes, not assumed. `--focus` and `--control-min: 44px` already exist.
13. `prefers-reduced-motion` honoured on the action-link hover transition; no `transition: all`.

### 9.2 Found, and deliberately NOT fixed here (Mit's call — §11)

- **Pin is buried behind a disclosure.** `fnd-progressive-disclosure` names this exactly: "Don't hide
  a setting most users need behind an 'Advanced' panel just to keep the primary screen looking
  clean — that's disclosure used to dodge an information-architecture decision." Pin is the highest-
  value action on this page and it is the strongest term in `sort_key`. The KB's suggested shape is
  Pin visible in the trailing column, Hide behind the disclosure. **Not done**: it changes an
  interaction this pass is not chartered to change, and it is a genuine design decision, not a
  defect fix.
- **`aria-pressed` on five mutually-exclusive chips is a semantics lie.** `projects.js` sets every
  other chip to `false` on click, so exactly one is ever pressed — but a screen reader hears five
  independent toggles and cannot infer the exclusivity. The correct semantics are a radiogroup /
  `aria-checked`. **Not done**: it would rewrite `projects.js`'s filter contract and the route tests
  that assert `data-projects-filter`, both explicit non-goals (§2).
- **Terracotta is both the brand accent and the `queued` status colour**, which `vis-color-accent`
  ("reserve the accent for the primary action and key emphasis") counts as a conflict. Inherited
  from `7a61923` and the Overview's `st-attention`; colour *values* are explicitly not relitigated.
- **The Almanac direction itself trips `anti-ai-default-look`.** That card names
  "a warm-cream background with a high-contrast serif and terracotta accent" and "a broadsheet
  hairline-rule layout" as two of three prohibited generated-design defaults. Recorded for honesty,
  **not acted on**: the direction was chosen deliberately by Mit over two rendered alternatives, and
  re-pitching directions is out of scope. It is worth him knowing the KB rates it a default.
- **The Overview's `01`/`02` heads are announced as "zero one, Needs attention."** This confirms the
  Overview pass's carried minor ("aria-hidden on the numerals") is a real `ux-copy-front-load`
  violation, not optional polish. Belongs to the Overview, not this page.

## 10. Verification

- `uv run pytest` green (979 baseline). New assertions: the fixed `Actions` summary text + its
  `aria-label`, and the status-edge mapping.
- `tests/test_contrast.py` stays green; **no new PAIRS entries are required** (§3.2). Any colour pair
  introduced beyond this spec must be added to `PAIRS` or it ships ungated.
- **Render-verify `/projects` at 1440 / 1280 / 1100 / 768 / 390, in both themes**, and measure
  `actionSpread == 0` rather than eyeballing alignment.
- **Render-verify the Overview's Recent projects rows are byte-for-byte unchanged in appearance**
  (§7) — the whole-plan review last pass found only cross-page consequences of shared code.
- After any `.py` change, kill and restart `bridge serve` (it keeps Python modules in memory and
  Jinja renders a missing key as `""`).

## 10a. Zero-results empty state

`ux-state-design-all` treats the zero-results empty as mandatory, and `ux-search-zero-results`
requires echoing the query back plus one concrete next action. Today `projects.html:55` renders a
static `No projects match your search.` — no query echo, no way out.

In scope, because it is this page's own state and the change is confined to the text `projects.js`
already writes: the message becomes query-aware (`No projects match "wirless".`) with a clear-search
affordance. This touches the empty-state text only — **not** `projectsMatchesFilter`, not the
`data-project-*` hooks, not the no-reload contract. Covered by the existing node harness in
`tests/test_static_js.py`.

## 11. Open — Mit's call at the tinker pass

Everything in §3 was decided in his absence and is cheap to reverse:

1. **Flat ledger vs grouped sections.** §3.1 argues the partition is wrong *in principle* (pin vs
   rank), not merely costly. If he wants groups anyway, the honest form is pin-respecting: a
   "Pinned" band first, then attention, then the rest.
2. **Status: edge + word vs the Overview's `st-*` phrases** ("Working now" / "Needs review").
   This pass chose the terse words for density; `st-*` buys maximum cross-page consistency.
3. **Density**: two-line ~64px vs a true one-line ~44px index (paths would truncate hard and the
   last-session title would have nowhere to live).
4. **The 36 `<details>` disclosures** — keep, or promote Pin to the visible trailing column per
   `fnd-progressive-disclosure` (§9.2). The KB rates burying Pin a named failure.
5. **Filter chip semantics** — `aria-pressed` toggles vs a true radiogroup (§9.2).
6. **The direction itself** — the KB rates cream + serif + terracotta a generated-design default
   (§9.2). Recorded, not acted on.
7. Carried from the Overview pass, unchanged and still his: the uncoloured schedule pills, the
   "Up next" section taxonomy, the section-hint typography, the recent-row arrow, and
   `aria-hidden` on the `01`/`02` numerals (now confirmed a real violation, §9.2).
