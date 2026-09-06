# Bridge audit — 2026-09-05 — findings and execution plan

Scope: full read of `src/bridge` (Python, JS, templates, CSS), the test suite, and the live
panel rendered at 1440px and 390px. Gate at audit time: `uv run pytest` → 1491 passed,
2 skipped, exit 0. Local `main` is 1 commit ahead of `origin/main` (132c55e, unpushed).

Legend: **[V]** = verified by hand (reproduced, traced, or measured), **[R]** = read-verified by
a reviewer against quoted source, not executed. Sizes: S ≤ 1h, M = a few hours, L = a session.

---

## 0. Headline

1. **The panel burns ~12% of a core doing nothing.** [V] `watcher.py` rglob+stats all 9,202
   transcript files every 0.5s; the live process showed 11.9% CPU averaged over 21h.
2. **Handoffs pile up and never go stale.** [V] Supersession is scoped to the *source session*
   (`store.py:556`), so any session started outside Bridge leaves its predecessor's handoff
   "Ready" forever. boardwatch shows six equal-weight "Ready" handoffs from 1h to 15h old.
3. **Every count means something different on every surface.** [V] Overview "Running 5" counts
   registry sessions (incl. unattributed and idle); Projects "Running 1" counts projects with a
   live session and no handoff. "Needs attention 17" counts items (six of them are one project's
   handoffs); the section shows 3 (`ATTENTION_LIMIT`) with no "14 more". The "Queued 10" chip
   filters to 2 rows.
4. **Commit 132c55e fixed the archived-project restore on one of three ingest paths.** [R] The
   spool drain and rebuild (`spool.py:247,313`) still queue under an archived card, and that is
   the path Durability calls "the normal case".
5. **The test suite writes into the real `~/.claude`.** [V] `test_workspace.py:314` omits
   `session_meta_dir`; my gate run left `~/.claude/usage-data/session-meta/s1.json` (removed).
6. **A live morph can attach the prompt editor to the wrong handoff.** [R] The handoff
   `<section>` is unkeyed and `data-live-preserve` on the `<details>` stops the morph walk, so
   after a handoff swap the textarea keeps the *old* `data-prompt-handoff` id while the title
   above it shows the new one; Save PATCHes a stale row.

---

## 1. Correctness bugs (fix first; each is a small, testable diff)

| # | Finding | Where | Fix | Size |
|---|---|---|---|---|
| C1 | Spool drain / rebuild never restores an archived project [R] | `spool.py:247,313` vs `routes_handoffs.py:68` | Extract one `ingest_handoff(store, h, project_id)` that does restore + create; call it from POST, drain, rebuild. Test: drain against an archived project → active. | S |
| C2 | `PATCH /api/handoff/{id}` has no status-transition guard; `consumed`→`queued` re-fires the prompt; prompt edits allowed on non-queued rows; `set_handoff_status` ignores rowcount [R] | `routes_handoffs.py:124-151`, `store.py:703` | Mirror `routes_schedule.patch_schedule`: 409 unless current status is `queued`; add `WHERE status=?` + rowcount check to the store primitive; log lost races like `finish_scheduled_run`. | S |
| C3 | Non-ASCII filenames are dropped from the staleness age but counted dirty (`core.quotepath` octal escapes) [V by reviewer: `café.txt`] | `gitprobe.py:74-77` | Use `git status --porcelain -z`: no quoting, NUL-separated, rename pairs are adjacent records. Removes the whole quote/arrow-stripping gotcha section. Tests: non-ASCII name, rename, space-in-name. | S |
| C4 | Brew self-update ignores `target_sha` then fails verification if upstream moved [R] | `update.py:324-331,352-368,436-440` | Re-resolve upstream HEAD immediately before install and treat *that* as the target; or accept "installed sha is an ancestor-or-equal of resolved HEAD" as success. Never report a working newer install as failure. | S |
| C5 | Config accepts `port = true` (bool ⊂ int); non-string list/dict entries raise raw `TypeError` outside the try block [R] | `config.py:146-171` | `type(x) is int` checks; validate element types; wrap in `ConfigError`. | S |
| C6 | Overview freshness strip reads a throwaway `RefreshCoordinator` that has never run, so it can never say "unavailable" while the sidebar can [V] | `overview.py:193`, `api.py:152` | Pass `app.state.refresh_coordinator` into `build_overview`; delete the per-request construction. | S |
| C7 | Unkeyed handoff `<section>` + `data-live-preserve` on the `<details>` → stale textarea identity after a morph [R] | `_launch.html:104-136`, `morph.js:10-13,44-49`, `liverefresh.js:41-46` | Give the section `data-key="{{ handoff.id }}"` (and schedule `<li>`s `data-key="{{ row.id }}"`) so a different id replaces the node instead of morphing into it; keep preserve semantics for same-id. minidom test: incoming fragment with a different handoff id replaces the textarea unless focused. | M |
| C8 | Deferred refresh (blocked by `protectedFocus`) is never retried after blur [R] | `liverefresh.js:56-59` | On `focusout` from a protected element, if `refreshRequested` call `schedule()`. Test with minidom activeElement change. | S |
| C9 | `_apply` ignores a `sessionId` change mid-file; the indexer's `prior["session_id"]` guard is vacuous after rehydrate [R] | `transcripts.py:69-74`, `indexer.py:198` | If `sid` present and `!= rec.session_id`: stop the record and treat the rest as a new session (or log + skip). Cheap guard. | S |
| C10 | Retry button removed without focus restoration (WCAG 2.4.3) [R] | `schedule.js:485-495` | Same pattern as `settleRow`/cancel: focus the row's `<summary>`/status before `.remove()`. | S |
| C11 | Loopback host check fails open on a missing `Host` header; every sibling check fails closed [R] | `api.py:114-121` | Reject when `host is None`. | S |
| C12 | Hermeticity: no guard on `Config.session_meta_dir` / `claude_projects_dir` / `discovery_paths` [V] | `tests/conftest.py`, `test_workspace.py:314` | Autouse fixture that points `Path.home()` (or those three defaults) at `tmp_path`; fix the one test. Assert the suite leaves `~/.claude` untouched. | S |

Suggested order: C12 → C1 → C2 → C3 → C7 → C8 → C6 → C4 → C5 → C9 → C10 → C11. One commit each.

---

## 2. Performance (the panel should be idle when idle)

| # | Finding | Where | Fix | Size |
|---|---|---|---|---|
| P1 | Watcher stats the whole corpus twice a second; 11.9% CPU over 21h [V] | `watcher.py:53-68` | Two-tier snapshot: (a) stat every *directory* mtime each poll (new files change the parent dir); (b) stat only files touched in the last N minutes ("hot set") each poll; (c) full walk on the existing ~15s reindex tick. Target: <0.5% idle CPU, same sub-second latency for appends to hot files. Measure before/after with `ps -o %cpu`. | M |
| P2 | `_link_background_launches` fetches ALL launches and ALL sessions per project on every reindex, forever, for any never-linked launch [R] | `indexer.py:147-156` | SQL `WHERE session_id IS NULL AND short_id IS NOT NULL`; stop retrying launches older than 24h (mark `unlinked`). Add this loop to the "proportional to delta" perf test. | S |
| P3 | `/project/{id}` builds every card (incl. git-cache reads) to find one, then re-sums token totals across all projects [V] | `workspace.py:131-140`, `api.py:511-517` | `build_card(store, cfg, project_id, ...)` single-project path; reuse the already-built card's `tokens_5h`. | M |
| P4 | N+1 `session_row` per queued handoff on every card build incl. SSE ticks [R] | `cards.py:440-455` | One `LEFT JOIN sessions` in `queued_handoffs`. | S |
| P5 | 32 sync SSE connections vs anyio's default 40-thread pool: a few open tabs starve page loads [R] | `api.py:290-312` | Either make `/events` an async generator (notifier → `asyncio.Event` via `call_soon_threadsafe`) or cap at 8 with a clear 503 message. Prefer async. | M |
| P6 | `bridge index` can run alongside `bridge serve` → two writers, read-modify-write races on `sessions` [R] | `__main__.py:27-59` | If the server answers on `cfg.port`, `bridge index` should POST `/api/refresh` instead of opening the DB; else take an `flock` on `bridge.db.lock`. | S |

---

## 3. Product and UX (what Mit actually looks at)

### 3.1 One vocabulary for counts [V]
- Create a single `counts.py` (or put it in `overview.py`) that both the Overview tiles and the
  Projects chips read. Unit is **projects**; handoff and session counts are secondary text
  ("2 projects · 10 handoffs", "1 project · 5 sessions").
- "Running" tile: attributed sessions only, or show "5 sessions in 1 project"; unattributed cwd
  sessions get their own line, not a place in the headline number.
- "N items need your attention" must match what is on screen: either render all items grouped
  by project, or say "Showing 3 of 17 · View all" with a working filter link.
- The "Queued 10" chip must select the same rows the "Queued 2" group shows. Same unit.

### 3.2 Handoff lifecycle: staleness and grouping [V]
- **Staleness signal:** a handoff is "possibly stale" when any session in the project started
  after `created_at` (the indexer already links launches; a plain `sessions.started_at >
  handoffs.created_at` query does it). Stale ones render demoted/collapsed with "a session has
  run since", not "Ready". Do **not** auto-supersede across sessions (the multi-handoff decision
  stands); demote visually and offer one-click Dismiss / Dismiss all stale.
- **Overview:** one card per project, "boardwatch · 6 queued", newest handoff summarised, not
  three cards for one project.
- **Project page:** first handoff full weight (prompt box + actions); the rest as compact rows
  (title · age · model/effort · Continue · Dismiss) that expand. Same primary control on every
  row (text button, not `▶` on rows 2..n). Add breathing room between one handoff's action row
  and the next's label.
- Placeholder title "boardwatch is ready to continue" when no session title: prefer the first
  line of the summary.

### 3.3 Small readability fixes [V]
- `needs_input` renders raw in Project state (`_workspace_current.html:40`): pass `live_state`
  through `status_label`. Reuse the `live_status` macro here instead of the hand-rolled block
  (`_components.html:20-30` vs `_workspace_current.html:40`).
- Labels repeat their values' suffix: "Usage today → 2.5M today", "Last 5h → 834k last 5h".
- Two "Connected" indicators (sidebar + page header). Keep the sidebar one.
- "Recent" pill on every row of *Recent projects* carries no information; drop it.
- "Project state indexed 0m ago" in Recent activity is system noise in a human activity list.
- The Session ended → Handoff ready → Next session strip is decorative; consider dropping or
  shrinking it to reclaim the hero card's height.

### 3.4 Navigation and shell
- **Mobile (<1024px):** the nav renders fully expanded above the content on every load and the
  "Menu" button is shown while the menu is already open [V]. Collapse by default below 1024px
  (CSS default + JS toggles; no-JS fallback keeps it visible). The `shell.js` header comment
  ("every nav click is a full page load") predates the persistent shell and is wrong.
- **Back button resets scroll to top** [R] `router.js:82-84,157-159`: save `.shell__body`
  scrollTop in `history.state` on push; restore on popstate; skip `announceArrival`'s scroll
  reset for pops.
- **View-transition CSS is dead on the primary path** [R] `app.css:283-286` + ~150 lines +
  `test_view_transitions.py`: the router intercepts every in-app click, and `pushState` doesn't
  trigger cross-document transitions. Decide: delete, or move to a real same-document
  `document.startViewTransition` around the swap (small, and would actually animate).

### 3.5 Registry noise [V]
- `/Users/mit` (the home directory) is listed as a project "mit". Auto-hide `$HOME` itself.
- `2026-09-08-session` is a subdirectory of boardwatch surfaced as its own project; Overview
  also shows two "Job apps/insta…" projects that truncate to the same path. Rule: a project whose
  path is inside another registered project's git worktree is folded into the parent (or at
  least badged "nested"). Show the *distinguishing* tail of a path, not a fixed-width prefix.

---

## 4. Decisions (answered by Mit, 2026-09-05)

| # | Question | Decision |
|---|---|---|
| Q1 | Scheduler after downtime (`claim_one_due` fires every overdue job on boot; `schedspool` rebuild rule 5 refuses retroactive fires). | **Fire if < 1h late; otherwise mark `missed`** and surface as an attention item with "Run now". Same rule on the live tick and the journal rebuild. |
| Q2 | CLI spools on *any* non-2xx incl. 422; drain skips `HandoffIn` validation. | **Spool only on connection error / 5xx.** On 4xx: exit 0, write to `spool/rejected/`, print the server's error loudly. Drain re-runs the pydantic model. |
| Q3 | Pinned outranks queued-handoff in `cards.sort_key`; undocumented. | **Keep; document in ARCHITECTURE.md.** |
| Q4 | Same-size, same-mtime rewrite invisible to the indexer. | **Keep; document the limit.** No fingerprint. |
| Q5 | No named gate; `falsify.py` unwired. | **Add a Makefile:** `make check` = `uv run pytest -q` (the gate); `make mutate` runs falsify over `tools/mutations` on demand. CI unchanged. |

## 5. Tests and docs

- Flakiness: `test_api.py:2226` sleep(0.5) for 32 threads; `test_watcher.py:26` thin debounce
  margin; `test_handoff_command.py:60` free-port TOCTOU; ~9 Node `subprocess.run` calls with no
  `timeout=`; `test_store.py:556` `join()` without timeout. [R]
- `test_shell_contract.py:166` asserts the substring `"preventScroll"` exists in router.js; it
  would pass with `preventScroll: false`. Assert the full token. [R]
- Scheduler single-winner is tested sequentially only; add a two-thread same-row claim test. [R]
- Doc drift to fix in ARCHITECTURE.md: "sole writer" (P6); "proportional to delta" (P1, P2);
  "card needs only first + trailing lines" (not implemented anywhere); `/handoff` writes
  `suggested_model` (the command never passes `--model`); pinning; middleware order note.
- Stale code: `agents.py` docstring says background records lack a pid (current registry
  writes `kind: "bg"` **with** a pid) and `DAEMON_ROSTER` is unused; `live.js:79-95` legacy
  frame path; `schedule.html:6-8` comment says "full page loads" but the route is swapped.

---

## 6. Suggested session sequencing

1. **Session A — bugs:** §1 in the listed order, one commit each, gate after each. (~1 session)
2. **Session B — perf:** P1 with a before/after CPU measurement, then P2–P4, P6. P5 last. (~1)
3. **Session C — counts + handoff lifecycle:** §3.1, §3.2. Mit reviews renders before polish. (~1)
4. **Session D — shell/UX polish + registry:** §3.3–§3.5, then §5 docs. (~1)
5. Q1–Q5 are answered (§4); nothing blocks Session A.

Reviewed but clean (no action): CSRF/same-origin guard on writes, AppleScript escaping and
argv construction, spool write atomicity, Jinja autoescape (no `|safe`), no inline handlers,
single `EventSource` per shell, universal `:focus-visible`, token contrast test coverage,
`HandoffIn.id` path-meaning validation (already tested at `test_api.py:614`).

---

## 7. Execution status

### Round 1 — 2026-09-05 evening, four lanes (personal account)
`main` 132c55e → 71dda24. Shipped all twelve §1 bugs, §2 P1–P4 and P6, the readability
items, and decisions Q1/Q2/Q5. Gate 1549 passed.

### Round 2 — same evening, seven parallel lanes (enterprise account)
`main` 71dda24 → 627f823. **Gate: 1615 passed, 2 skipped.** Every lane was reviewed diff by
diff, rebased, fast-forwarded, and re-gated in the canonical worktree. Three rebase conflicts
(all "both lanes appended tests to the same file") and one genuine semantic conflict, resolved
by hand. Nothing is pushed.

**Closed this round**
- **P5** — `/events` is an async generator; `ChangeNotifier.wait_async` parks on a loop-bound
  `asyncio.Event` woken via `call_soon_threadsafe`; blocking builds go through the threadpool
  per frame instead of pinning a worker for the connection's life. Cap 32 → 64. The lane's own
  first "loop is not blocked" test passed against the unfixed code, so it was rewritten.
- **Idle CPU, the real result.** The watcher rewrite in round 1 was not the fix. `transcript_files`
  globs one level deep, but the watcher walked the whole subtree, so writes to nested
  `<session>/subagents/` transcripts fired a full reindex ~1.4×/second, every run scanning zero
  files. `indexer.indexed_dirs` now gives both the same scope, and the hot-directory re-list
  window dropped from 300s to 2s. **Measured on the real panel: 9.1% → 2.2% of a core, idle,
  no client.** One client adds ~2 points.
- **§3.1 counts** — one shared vocabulary in `overview.py`; every tile and chip counts PROJECTS,
  with secondary magnitudes as captions. Verified live: Overview and Projects now both read
  Running 1, Needs attention 9, Queued 2 (they read 5/17/10 versus 1/9/10 before).
- **§3.2 handoff staleness** — `session_since` rides the existing query; an overtaken handoff is
  demoted, collapsed, and says so; every row gets the same labelled launch button; "Dismiss all
  N overtaken" with honest partial-failure reporting; titles prefer the summary's first line.
- **§3.5 registry noise** — home is never a project; a non-worktree directory nested inside a
  registered worktree is auto-hidden (reversible); paths clip the shared prefix, not the leaf.
- **§3.4 view transitions** — the ~150 lines of CSS were dead under a client-side router. The
  swap now runs inside `document.startViewTransition`. Verified in a real browser: one
  transition per nav click, with real animations in the pseudo tree.
- **F7/F8** — legacy live-frame path deleted (its three tests rewritten against schema-1 frames,
  not dropped); two stale comments corrected.
- **§5 + docs** — deterministic replacements for four timing-luck tests, node subprocess
  timeouts, a genuine two-thread scheduler contention test, and the four ARCHITECTURE sentences.

**Coordinator fixes on top**
- `direction: rtl` left-clipping reordered a path's leading slash: `/Users/mit/dev/bridge` was
  rendering as `Users/mit/dev/bridge/`. Fixed with a generated U+200E; caught in the browser,
  test proven non-vacuous.
- A mutation reported as a surviving gap was a mis-pointed spec: the behaviour was guarded by a
  different test all along. Re-pointed, 4/4 caught.
- ARCHITECTURE's Liveness section credited the tiered poll with a saving it had not made;
  rewritten around the scope rule and the real numbers.

**Verified in a real browser** (Chromium against the live panel): C7 keyed morph both directions
(different id replaces and re-binds the editor; same id preserves an in-progress edit and focus);
C8 deferred refresh (0 fetches while focused, retried on blur); §3.4 transitions; Back restoring
scroll inside the transition wrapper; the narrow-width nav collapsed on first paint; zero console
errors.

### Still open
- **C10** was never browser-verified: rendering a Retry button needs a FAILED scheduled run, and
  manufacturing one risks a real launch on Mit's machine. Guarded by tests and by reuse of the
  same focus helper the cancel path already uses.
- **Reindex is still O(corpus) per change (~106 ms)**, which is most of the remaining 2.2%. The
  watcher knows which files changed and discards that. Two cheap wins inside it: one bulk
  `SELECT * FROM scan_state` instead of 9,202 single-row reads (~29 ms), and sorting
  `transcript_files` by name rather than by `Path` (~8 ms).
- **`agents.probe()` spawns `/bin/ps` on every SSE rebuild** (~10.9 ms, up to 5/sec/tab under a
  bump storm). A TTL cache trades against liveness accuracy — a judgement call, not a bug.
- **The 15s periodic reindex duplicates the watcher's 15s reconcile.** Collapsing them changes
  `generation`/freshness semantics the SSE full-update depends on.
- **`ChangeNotifier.wait()` now has no callers in `src/`** — deletion candidate.
- **`data-live-path`** is rendered by two templates and read by nothing since the F7 deletion.
- **`dashboard._unattributed`** still uses an exact-path test, so it can list a subdirectory
  session the cards already attribute.
- **A pinned project holding a queued handoff** sits in the Pinned group, so the Queued group
  header can read lower than the chip while the chip's filter still shows the row.
- **§3.3** (the decorative Session-ended → Handoff-ready strip) and the weight of a demoted
  handoff's action row are taste forks left for Mit to see rendered.
- Mit's live database had three noise rows the new rule could not touch (it only judges rows a
  run creates). `/Users/mit` and the nested `.agent/2026-09-08-session` were hidden through the
  API, reversible from the Hidden drawer; the agent worktree rows auto-archive.
- **Four mutation specs have a real survivor** (5 mutations, 415/422 caught). They were
  invisible until `make mutate` was fixed to run past its self-check: an unknown launch mode
  reaching the launcher instead of being refused at the API edge (`task2-api`), and three
  diagnostics-affordance behaviours (`phase4-task2`, `phase4-task7`, `project-lifecycle`) —
  always-shown, never-shown, and an index that survives a diagnostics write failure. Each is an
  untested behaviour, not a broken one.
- **Not pushed.** `main` is 50+ commits ahead of `origin/main`.
