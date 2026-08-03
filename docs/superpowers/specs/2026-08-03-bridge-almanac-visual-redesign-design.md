# Bridge Visual Redesign — "Almanac" Design System

**Date:** 2026-08-03
**Status:** Approved direction, pending spec review
**Scope of this spec:** Establish the Almanac visual system and land it on the **Overview** page (light + dark). Define the token/type/component system so later pages inherit it.
**Branch (implementation):** to be cut from `feat/bridge-product-redesign` (e.g. `feat/bridge-almanac-redesign`).

---

## 1. Problem

Bridge's information architecture is strong — the "12 items need your attention, everything else can wait" triage model is a real point of view, and the codebase is disciplined (3-layer token system in `app.css`, WCAG contrast measured per color, clean Jinja structure). But the **surface** reads as a generic dark developer dashboard:

- One teal accent (`#176579`) on near-black — indistinguishable from a hundred other tools.
- No depth: cards (`#192326`) barely lift off the canvas (`#12191b`).
- Flat hierarchy: page title is large, then almost everything below is the same weight/size.
- Weak, inconsistent status pills floating in awkward center columns.
- Ghost-box buttons; Atkinson Hyperlegible reads as a plain grotesque at UI sizes, paying an identity cost with no personality return.

**User intent (verbatim):** *"I don't want the cliché techy vibe … it should feel good to look at … make you feel like you are in control and know everything that's going on. Everything organised."*

## 2. Goals / Non-goals

**Goals**
- A distinctive, warm, **editorial** identity ("control almanac") that is unmistakably Bridge — not a dark-dashboard template.
- A stronger sense of *command*: you can see the whole system state at a glance and always know what is waiting on you.
- Real typographic and spatial hierarchy; depth and organization without visual noise.
- Preserve accessibility (WCAG 2.2 AA) and the existing token architecture — this is a **re-theme + targeted additions**, not a rewrite.
- Ship both a light theme and a **separately-designed warm dark theme** (not an inversion).

**Non-goals**
- No change to the information architecture, navigation structure, or routes.
- No build system / framework introduction — stays vanilla Jinja + hand-authored `app.css` + vanilla JS.
- No backend logic changes beyond exposing already-computed counts/metadata to the Overview template.
- Restyling every page is **out of scope for this spec** — Overview first; roll-out tracked in §10.

## 3. Direction — "Almanac"

A warm, editorial control surface: cream paper (light) / espresso paper (dark), a characterful display serif for headlines and numerals, monospace for machine data, generous rules and numbered sections. Calm to rest in, organized enough that you always feel on top of everything.

Chosen over two rejected alternatives (both explicitly ruled out by the user as "cliché techy"): **A — Blueprint** (drafting/instrument) and **B — Command deck** (luminous dark cockpit). Rendered comparison mockups exist in the working scratchpad; the approved refined Overview is "Almanac v2" (light + night).

**Guiding metaphor:** a well-kept almanac / ledger of your work — everything recorded, dated, and in its place.

## 4. Typography

Three roles. Two of the three faces are **already shipped** — only one new font is added.

| Role | Face | Status | Used for |
|---|---|---|---|
| Display | **Fraunces** (variable serif, "old-style with attitude") | **NEW** — self-host woff2 subset | H1/H2 headlines, section titles, card titles, project names, command-strip numerals |
| Text / UI | **Atkinson Hyperlegible Next** | existing (`static/fonts/`) | Body, labels, buttons, descriptions, table cell text |
| Mono | **IBM Plex Mono** | existing (`static/fonts/`) | Paths, slugs, status tags, metadata, numeric counts' unit labels |

Rules:
- Serif is used **at display sizes only** (≈19px and up). It never sets body copy — this is what keeps dense pages (Projects' 36 rows, Schedule tables) crisp.
- Build hierarchy with real size **and** weight contrast (see [[anti-ai-default-typography]]): display serif 27–56px semibold vs. 13–14px Atkinson body.
- Numerals in the command strip and tables use **tabular/lining figures**.
- Body line length 45–75 chars; never justify; never center long body copy.
- Ship Fraunces with an OFL license file + `PROVENANCE.md` entry, matching the existing fonts folder convention. Preload the display weight to avoid FOUT on headlines.

> Mockup note: the rendered mockups used Georgia (for Fraunces) and Avenir Next (for Atkinson) as local stand-ins. Production uses Fraunces + Atkinson + Plex Mono.
>
> Open risk (§11): if Atkinson reads too utilitarian beside Fraunces, evaluate a warm humanist text face. Default is **reuse Atkinson** (no new dependency, keeps the a11y win).

## 5. Color

Warm palette in both themes, folded into the existing **primitive → semantic → component** layers in `app.css`. The accent shift (teal → terracotta) happens at the **primitive** layer so it cascades through every component that already references the semantic/legacy tokens.

Starting values below are the design intent — **every pairing must be contrast-verified at build** (§9), and `tests/test_contrast.py` must stay green.

### 5.1 Primitives — light (cream)
```
--p-canvas:  #f5efe3   canvas paper
--p-surface: #fcf8f0   raised card
--p-text:    #211d17   ink (near-black, warm)
--p-text-2:  #6b6152   muted ink
--p-rule:    #dcd2bf   hairline rule
--p-accent:  #b0421d   terracotta — brand / interactive / needs-input
--p-run:     #2f6b46   forest — running / live / success
--p-review:  #9a6a14   gold — needs review / pending / warning
```
### 5.2 Primitives — dark ("night almanac", espresso — NOT an inversion)
```
--p-canvas:  #1b1714
--p-surface: #241f1a
--p-text:    #efe6d6
--p-text-2:  #b3a691
--p-rule:    #3a332a
--p-accent:  #db7048   terracotta, lightened + to be desaturated for dark
--p-run:     #74b98d   forest, lightened
--p-review:  #cf9a3f   gold, lightened
```
Rail (`--p-nav`) stays a dark warm espresso in both themes (like today's invariant nav), with its own text/accent primitives re-checked for contrast against it.

### 5.3 Semantic mapping
- `--accent` / interactive / focus → `--p-accent` (replaces teal `--work` as the primary/interactive color).
- Status roles are **first-class**: `--status-running → --p-run`, `--status-review → --p-review`, `--status-attention → --p-accent`.
- Legacy semantic aliases (`--bg`, `--fg`, `--muted`, `--line`, `--card`, `--accent`, `--risk-*`, `--work*`) are **repointed**, not removed, so untouched components keep working. `--work`/`--work-soft` retire to aliases of the new running/accent roles as appropriate (resolved in the plan).

### 5.4 Status taxonomy (color is never the only signal — WCAG 1.4.1)
Every status carries **three** redundant cues: a text label, a mono slug, and color (card top-border + tag). Never color alone. See [[vis-color-not-sole-signal]].

| Status | Color role | Text label | Card top-border |
|---|---|---|---|
| Running / working | forest | "Working now" | forest |
| Needs input | terracotta | "Needs input" | terracotta |
| Needs review | gold | "Needs review" | gold |
| Queued | muted | "Queued" | rule |
| Failed | terracotta (strong) | "Failed" | terracotta |
| Stale | muted | "Stale" | rule |

## 6. Layout & structure (Overview)

Top-to-bottom:

1. **Masthead** — eyebrow ("Local control plane"), large Fraunces `Overview`, italic serif subtitle ("12 items need your attention. Everything else can wait."), right-aligned connection status + Refresh. Closed by a **double rule**.
2. **At-a-glance command strip** (NEW) — a 6-cell ruled band of the whole system state: `Running · Needs input · Queued · Dirty trees · Scheduled · Projects`, each a big Fraunces numeral + mono label. Running/attention numerals take their status color. This is the "I know everything / in control" surface.
3. **`01 Needs Attention`** — numbered section header + italic hint, over a 2-column grid: a tall **hero card** (the single most-attention-needing item, enriched with `Last activity`, `Waiting on`, primary + ghost actions, path footer) and stacked **mini cards** (other attention items). Status shown via top-border + tag.
4. **`02 Recent Projects`** — editorial list rows: Fraunces name + mono path, branch/dirty tag + description, `Open →`. Hairline row rules only.
5. (Existing Overview content preserved: Up next / scheduled, usage & index details — restyled into the system.)

Spacing on the existing 4px scale; concentric radii; consistent vertical rhythm.

## 7. Components

- **Card** — warm raised surface, 1px rule, **colored top-border = status**. Hero variant spans two rows and carries detail rows (`key` in mono small-caps + value). Depth comes from the border + surface step + (light) a very soft shadow; dark uses **lightness for elevation**, not shadow.
- **Command-strip cell** — ruled divider, Fraunces numeral, mono label; status color on live/attention numerals.
- **Buttons** — primary = solid terracotta (dark-mode primary uses dark ink text on lightened terracotta to hold contrast); secondary = ghost (terracotta outline); one primary action per view.
- **Status tag** — mono, uppercase, tinted soft background; always paired with a text meaning.
- **List / table row (dense pages)** — per [[ux-table-scannable]]: whitespace + hairline row rule + hover/zebra; **no full cell grid**; increase padding before adding borders; headers carry weight, not a ruled line; right-align numerics with tabular figures.
- **Icons** — real SVG with accessible names (reuse existing nav icon approach). The `◧ ▦ ▤` glyphs in mockups are placeholders; never ship unicode/emoji as icons.

## 8. Motion

Minimal and warm. Hover/focus transitions on `--motion-fast`/`--motion` (existing tokens), animating `transform`/`opacity` only. No entrance choreography on data. Honor `prefers-reduced-motion: reduce` (disable non-essential transitions).

## 9. Accessibility contract (build gates)

1. **Contrast** — every text pair ≥ 4.5:1 (≥ 3:1 large text / UI shapes) in **both** themes; `tests/test_contrast.py` stays green. Special attention: terracotta-as-text on cream, muted ink on cream, all status tags, dark-mode accent-on-surface.
2. **Dark accents desaturated + lightened**, then re-verified — no saturated fills blooming on espresso (see [[vis-dark-desaturate]], [[vis-dark-not-invert]]).
3. **Status never color-only** — text label + slug always present (§5.4).
4. **Focus** — visible focus ring on every interactive element; keyboard operable.
5. **Targets** — ≥ 24px (WCAG 2.5.8); keep existing `--control-min` (2.5rem) for primary controls.
6. **Reduced motion** honored.
7. **Icons** carry accessible names.

## 10. Implementation approach & rollout

**Overview first (this spec):**
1. Add Fraunces webfont (subset woff2 + `@font-face` + license/provenance); wire the FOUC-guard/preload.
2. Re-theme `app.css` primitives (light + dark blocks + `[data-theme]` overrides) to the Almanac palette; repoint semantics/legacy aliases; introduce status roles.
3. New components: command strip, restyled cards + status top-borders, editorial list rows, buttons.
4. Template edits: `base.html` masthead/rail, `overview.html` command strip + enriched hero + sections. Expose the six counts (already computed for the Projects filter pills — `All / Needs attention / Running / Queued / Hidden`) plus dirty/scheduled to the Overview context. Hero `Waiting on` uses available session metadata and **degrades gracefully** when the reason is unknown.
5. Verify: `uv run pytest` green (incl. contrast + static-JS tests), manual light/dark screenshot check.

**Then roll the system out** (separate specs/PRs, in order): Projects → Schedule → Settings → Diagnostics → project detail. The dense-table pattern (§7) governs Projects/Schedule.

## 11. Risks & open questions

- **Atkinson vs. a warmer text face** next to Fraunces — default reuse Atkinson; revisit only if it reads utilitarian (§4).
- **Terracotta contrast** as text on cream is the tightest pairing; may need to darken `--p-accent` slightly for text use vs. fills (consider a separate `--accent-text` primitive).
- **Hero "Waiting on" data** may not be precisely available from session state; treat as progressive enhancement.
- **Dark terracotta button text** — confirm dark-ink-on-terracotta beats white-on-terracotta for AA at button sizes.

## 12. Success criteria

- Overview reads as unmistakably Bridge, passes the [[anti-ai-generic-look]] smell test ("would I ship this for any other product?" → no).
- The at-a-glance strip lets you state the whole system's status in one sentence without scrolling.
- Full WCAG 2.2 AA in both themes; `test_contrast.py` and the suite green.
- The token/type/component system is reusable as-is by the remaining pages.
