# Bridge — Handoff

**Date:** 2026-07-31
**Branch:** `phase1-read-only-panel` — 29 commits, 93 tests passing, tree clean
**Status:** Phase 1 complete and working, **plus path aliasing**. **Not merged.**
Phase 2 is planned but not started.

There is an irony worth noting: this file exists because Bridge's Phase 2 — the
handoff loop that would store this automatically — isn't built yet. This is the
last handoff that has to be a stray markdown file.

## What works today

```bash
cd ~/dev/bridge
uv run python -m bridge index    # 7,706 transcripts: ~11s cold, ~0.2s rescan
uv run python -m bridge serve    # http://127.0.0.1:8787
uv run pytest                    # 81 passing
```

Against the real corpus: **35 project cards** from 421,480 parsed lines, 0 parse
errors. 8 cards `not a git repo`, 6 showing the ⚠ staleness warning. WCAG AA
contrast verified in both light and dark. No probe failure can block rendering.

Eight modules, each independently tested: `config`, `models`, `transcripts`,
`store`, `gitprobe`, `registry`, `indexer`, `cards`, plus `api` and `__main__`.

## Decided this session

**Path aliasing is approved** — merge split project history rather than archiving
the old halves. Seven verified mappings plus one archive are specified in
`docs/superpowers/specs/2026-07-31-bridge-control-panel-design.md` § "4b. Path
aliasing (Phase 2)". Most work now happens in `~/dev`, so `~/dev` paths are
canonical.

Key enabling fact: **the database is a pure derived cache.** Re-attribution needs
no migration — delete `~/.bridge/bridge.db` and re-index (~11s).

## Closed since the last handoff

1. **Path aliasing — done** (`bed0b3a`, `7c484d1`). Seven old `~/Documents/...`
   cwds now resolve through a `project_aliases` table before `upsert_project`;
   `Vandit & Zeel/VANDITZEEL` is archived through `set_project_status`, which is
   now reachable. Against the real corpus: **35 cards → 29**, 0 parse errors,
   ~10s. Job apps 6922+576=7498, StreakSync 17+1=18, anghkooey 2+6=8, projectX
   2+3=5, dota2 2+1=3; `hookrail` and `Houston social` are new canonical cards.
   The mappings live in `config.DEFAULT_ALIASES`, seeded into the table on every
   index, so a rebuilt database re-applies them.
2. **The flaky test is resolved** (`5102420`). Measured, not guessed:
   reintroducing the `store.conn`-bypasses-the-lock bug fails
   `test_concurrent_mixed_routes_do_not_error` **10 times in 20**, and it never
   fails on correct code (0/5). It is kept as a smoke check with that rate in
   its docstring, and the invariant it was reaching for is now asserted
   deterministically by `test_no_module_outside_store_touches_the_raw_connection`
   (fails 5/5 when violated).

## Open items

1. **Merge decision** for `phase1-read-only-panel`. Mit's call; nothing is
   blocking it now.
2. **Four Phase 2 decisions** were put to Mit and timed out unanswered. They are
   implemented as assumptions in the Phase 2 plan's first table — queue
   semantics, server uptime model, Phase 2's usable surface, and backfill.
   Confirm before Task 2.

## Advisory, from the final whole-branch review

- `git_cache` table is created but never read or written, so the spec's "last good
  git state, with its age" on probe timeout is unimplemented; a timed-out card
  just says "git unavailable".
- `cfg.dev_dir` is unused — discovery is transcript-directories-only, not the
  spec's "also `~/dev/*` git repos".
- Rescan is ~0.22s against a stated 200ms target. `Card.spark` is an unused
  Phase-4 stub. `serve` never calls `store.close()` (harmless at exit).

## Process that must carry forward

**Seven tests in this session passed while constraining nothing**, and all seven
were fixture-design errors in the plan, not implementation errors. The suite was
green at 10/10, 12/12, 18/18 and 78/78 with hollow tests in place. Green was never
once informative.

What actually worked, and should be standard for Phases 2–4:

- **Falsification.** For every test guarding a load-bearing behaviour, mutate the
  implementation and require the test to fail, pasting real output. Demand
  observed failures; "this would fail if…" claims were wrong roughly half the
  time.
- **Run against the real corpus, not just fixtures.** Three real bugs were found
  only this way.
- **Mutate the real file and `git checkout --` to restore.** A scratch copy with
  `PYTHONPATH` does *not* override the venv-installed `bridge` package and will
  silently test unmutated code. The tell is that the control run also fails.
- **Commit the implementation before falsifying it.** `git checkout --` restores
  to HEAD, so mutating an uncommitted implementation deletes it at the first
  restore, and every later mutation is measured against a missing feature.
- **Disable bytecode caching in the harness** (`PYTHONDONTWRITEBYTECODE=1`, clear
  `__pycache__` between runs). A mutation that only *moves* code is byte-size
  identical to the original, and `git checkout` restores it within the same
  second. CPython validates a `.pyc` by (source mtime, source size) at one-second
  granularity, so both match and the stale bytecode compiled from the *mutated*
  source keeps running in later processes. This cost an hour during path
  aliasing: it presented as a real archiving bug, reproduced consistently, and
  survived both a source read and `inspect.getsource` — which show the correct
  file while the wrong bytecode executes.

Three bugs this discipline caught, for calibration:

- `_git` stripped `git status --porcelain`, destroying the leading space in
  ` M path`. Every unstaged-modified file was silently dropped from the staleness
  age while still counted in `dirty_count` — so ⚠ would never have fired for the
  most common git state. Two prior reviews read that function and missed it.
- A single SQLite connection shared across FastAPI's worker threadpool: 196 of 200
  concurrent requests failed. The spec reasoned about cross-*process* concurrency
  and never considered intra-process threading. The Task 4 test that "proved" the
  architecture used four *separate* connections — the case WAL genuinely handles.
- The first fix for that was then verified by scanning only `store.py`, missing
  three call sites in `api.py` and `indexer.py` that bypassed the lock.

Also: an optimization the plan mandated (an attachment byte-prefix filter) was
completely inert — 0 hits across 13,796 records — because real transcript lines
put the payload before the record's own `type` key.

## Ledger

Full per-task history, every finding, and both controller process errors:
`.superpowers/sdd/2026-07-31-bridge-phase1-read-only-panel/progress.md`

## Phases remaining

2. **Handoff loop** — `bridge` CLI, `/handoff` command, spool-on-server-down.
   This is the phase that solves the original problem. **Planned:**
   `docs/superpowers/plans/2026-07-31-bridge-phase2-handoff-loop.md`. Note the
   plan's finding that Phase 2 breaks the "database is a pure derived cache"
   invariant — handoffs are the first authored data Bridge stores — and keeps it
   by treating the spool as a retained append-only journal.
3. **Launcher** — new terminal + `--bg`, with `--session-id` pre-assignment so a
   launched session is followed back into its transcript.
4. **Live** — SSE, `claude agents --json` probe, sparklines, diagnostics.

Each needs its own spec-derived plan. Phase 1's plan is at
`docs/superpowers/plans/2026-07-31-bridge-phase1-read-only-panel.md`; note its
Task 4/7/9 code blocks are stale relative to what shipped — git is authoritative.
