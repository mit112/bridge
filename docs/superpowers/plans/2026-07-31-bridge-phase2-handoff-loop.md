# Bridge Phase 2 — Handoff Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`)
> syntax for tracking.

**Goal:** End a session by running `/handoff`, and the next-session prompt is captured, attached to
its project, and retrievable — from the panel or the terminal. This is the phase that solves the
original problem: prompts currently get juggled by hand, and this file's predecessor, `HANDOFF.md`,
exists only because the loop does not.

**Architecture:** The FastAPI process stays the sole writer. A new thin `bridge` CLI is the only
thing Claude sessions touch; it speaks HTTP to that server and never opens the database. When the
server is unreachable the CLI writes a spool file and exits zero, and the server drains the spool on
boot. Capture and retrieval only — editing-before-launch and the launcher itself are Phase 3.

**Spec:** `docs/superpowers/specs/2026-07-31-bridge-control-panel-design.md` §6, §8, and
"Data flow: the handoff loop".

**Builds on:** Phase 1 at `phase1-read-only-panel` (93 tests). Path aliasing landed in `bed0b3a`;
`bridge handoff` run from an old `~/Documents/...` cwd must resolve through the same alias table.

---

## Decisions taken without an answer

These four were put to Mit and timed out unanswered. Each is implemented as stated below and is
cheap to reverse; **confirm before Task 2 starts.**

| # | Decision | Taken | Why | Reversal cost |
|---|---|---|---|---|
| 1 | Second `/handoff` for a project that already has one queued | **Supersede** — new becomes queued, old marked `superseded`, history kept | Matches the spec's singular `GET /api/handoff/{project_id}`; a card shows exactly one next-step. Refusing would fail a session at its last step | One status value + one query |
| 2 | Server uptime model | **Manual `bridge serve`; spool is the normal path** | No launchd plist, no restart story. Consequence: spool is load-bearing and gets tested as a primary path, not an edge case | Adding launchd later changes nothing in the code |
| 3 | What makes a queued handoff useful before the Phase 3 launcher | **Copy-to-clipboard on the card + `bridge next` printing to stdout.** Inline editing deferred to Phase 3 | Phase 2 is "never lose a prompt, always retrieve it"; editing earns its place next to a launch button, not before one | Additive |
| 4 | Backfill of stray `HANDOFF.md` / `NEXT-SESSION.md` | **Included, as the last task, explicitly droppable** | Populates the panel on day one and exercises the capture path against messy real input | Delete one task |

---

## Global Constraints

Phase 1's constraints all carry forward. Restated where Phase 2 can violate them, plus new ones:

- **Bridge never writes to a user project repo.** Phase 2 adds a second writable location and no
  more: `~/.bridge/spool/`. The `/handoff` command writes nothing to disk itself.
- **A session must never fail because the panel is down.** `bridge handoff` exits zero on every
  reachable-server failure mode. This is the single most important property in the phase.
- **The prompt is never interpolated into a shell string or argv.** It arrives on stdin via
  `--prompt-file -`. Prompts contain quotes, backticks, newlines, and `$`.
- **The CLI generates the handoff UUID**, not the server. This is what makes spool drain idempotent:
  a re-drained file collides on primary key and is ignored rather than duplicated.
- The CLI opens no database. Its only dependency beyond stdlib is `httpx`.
- Migrations remain **additive only** — new tables and columns, never a rebuild. The database stays
  a derived cache for transcript-derived data, but **handoffs are NOT derived** — they are the first
  authored data Bridge stores, and `rm ~/.bridge/bridge.db` now destroys real user data. Task 1
  must confront this directly.
- Absolute coreutil paths in any shell-out. Bind `127.0.0.1` only.
- WCAG 2.2 AA on new UI: the copy button needs a visible focus ring, an accessible name, and a
  status message that is not conveyed by color alone.

---

## The derived-cache invariant breaks here

Phase 1 could say "the database is a pure derived cache; delete it and re-index in ~10s." That
sentence justified path aliasing needing no migration, and it is now **false for the `handoffs`
table**. A queued prompt that a session spent its last tokens composing cannot be regenerated from
transcripts.

Task 1 resolves this by keeping the property rather than abandoning it: every handoff is written to
`~/.bridge/spool/<uuid>.json` **and** the database, and spool files are retained after drain rather
than deleted (moved to `~/.bridge/spool/drained/`). The spool becomes an append-only journal that
can rebuild the `handoffs` table, so `rm bridge.db && bridge index` remains safe. Any design that
deletes the spool file on successful drain forfeits this and must be rejected in review.

---

### Task 1: `handoffs` table, store methods, and the spool journal

**Files:** modify `src/bridge/store.py`; create `src/bridge/spool.py`; test `tests/test_store.py`,
`tests/test_spool.py`

**Interfaces:**
- Produces `Store.create_handoff(h: Handoff) -> str` (returns id; supersedes any existing queued
  handoff for the project in one transaction), `Store.queued_handoff(project_id) -> Row | None`,
  `Store.handoffs(project_id, limit) -> list[Row]`, `Store.set_handoff_status(id, status)`.
- Produces `spool.write(handoff) -> Path`, `spool.drain(store) -> DrainStats`,
  `spool.pending_count() -> int`.
- Schema (additive): `handoffs(id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES
  projects(id), source_session_id TEXT, summary TEXT, next_prompt TEXT NOT NULL, suggested_model
  TEXT, suggested_effort TEXT, status TEXT NOT NULL DEFAULT 'queued', created_at INTEGER NOT NULL,
  consumed_at INTEGER)`, `status ∈ {queued, consumed, dismissed, superseded}`, plus
  `CREATE INDEX idx_handoffs_project ON handoffs(project_id, status, created_at)`.

**Steps:**
- [ ] Add the table to `SCHEMA` and the store methods. Supersede-and-insert happens inside one
      `store.transaction()`, so a crash cannot leave a project with zero queued handoffs.
- [ ] `spool.write` serializes to `~/.bridge/spool/<uuid>.json` with `os.replace` from a temp file
      in the same directory, so a reader never sees a partial file.
- [ ] `spool.drain` reads every `*.json`, inserts with the file's own id, and moves the file to
      `drained/`. A file that fails to parse moves to `spool/bad/` and never blocks the drain.

**Tests (each requires an observed failure from a mutation):**
- [ ] A second `create_handoff` for the same project supersedes the first: the old row's status is
      `superseded`, `queued_handoff` returns the new one, and both survive in `handoffs()`.
      *Mutation: drop the supersede UPDATE → the test must see two queued rows.*
- [ ] Supersede is atomic: if the insert fails, the previous handoff is still `queued`.
      *Mutation: move the insert outside the transaction.*
- [ ] `drain` is idempotent: draining the same spool directory twice yields one row.
      *Mutation: have the server mint its own id instead of using the file's → duplicates.*
- [ ] A corrupt spool file lands in `bad/` and the valid files in the same run still drain.
      *Mutation: let `json.JSONDecodeError` propagate.*
- [ ] Drained files are retained, not deleted, and `rm bridge.db` + re-drain rebuilds the table.
      *Mutation: `unlink` after drain → the rebuild yields zero handoffs.*
- [ ] `spool.write` never leaves a partial file: write a large prompt and assert the temp name is
      gone and the JSON parses. *Mutation: write in place without `os.replace`.*

---

### Task 2: Handoff API routes

**Files:** modify `src/bridge/api.py`; test `tests/test_api.py`

**Interfaces:** `POST /api/handoff` (body: `id`, `project_path`, `session_id`, `summary`,
`next_prompt`, `suggested_model`, `suggested_effort`) → `201 {id}`;
`GET /api/handoff/{project_id}` → queued handoff or `204`;
`PATCH /api/handoff/{id}` (body: `status`) → `200`.

**Steps:**
- [ ] `POST` resolves `project_path` through the **alias table** and then `upsert_project`, so a
      handoff from an unindexed or moved project attaches correctly rather than 404ing.
- [ ] `POST` is idempotent on `id` — a spool drain and a live POST of the same handoff cannot
      both insert.
- [ ] Drain the spool once at app startup, before serving. Startup must not fail if the spool is
      unreadable.

**Tests (falsification required):**
- [ ] POST from a path that only exists as an **alias** attaches to the canonical project.
      *Mutation: skip the alias resolution → it attaches to the old path and splits history again.*
- [ ] POST from a path with no project row creates one. *Mutation: 404 instead of upsert.*
- [ ] POST with the same id twice yields one row and both calls return 2xx.
- [ ] A prompt containing quotes, backticks, newlines, `$(...)`, and a 40 KB body round-trips byte
      for byte through POST → DB → GET. *Mutation: truncate or shell-escape.*
- [ ] Boot-time drain: put a file in the spool, create the app, assert the handoff is queued.
      *Mutation: remove the startup drain call.*

---

### Task 3: The `bridge` CLI

**Files:** create `src/bridge/cli.py`; modify `pyproject.toml` (console script `bridge`),
`src/bridge/__main__.py`; test `tests/test_cli.py`

**Interfaces:**
```
bridge handoff --summary <text> --prompt-file - [--project P] [--session-id S] [--model M] [--effort E]
bridge next [--project P]          # prints the queued prompt to stdout, nothing else
bridge status [--project P]
bridge open
```

**Steps:**
- [ ] `--project` defaults to `$PWD`. The CLI sends the raw path; the **server** resolves aliases.
- [ ] `bridge next` prints the prompt and nothing else, so `claude "$(bridge next)"` works. Exit 1
      with an empty stdout and a stderr message when nothing is queued, so the shell substitution
      does not silently launch an empty prompt.
- [ ] Connection refused, timeout (2s), or any 5xx → `spool.write`, print the spool path to stderr,
      **exit 0**.

**Tests (falsification required):**
- [ ] With no server, `bridge handoff` exits **0** and a spool file exists with the right content.
      *Mutation: let `httpx.ConnectError` propagate → non-zero exit.* This is the property the
      whole phase rests on; test it against a genuinely closed port, not a mock.
- [ ] A 500 from the server also spools and exits 0. *Mutation: only catch connection errors.*
- [ ] A 2xx does **not** spool. *Mutation: always spool → an unnecessary journal file appears.*
- [ ] `bridge next` with nothing queued exits non-zero and prints nothing on stdout.
      *Mutation: exit 0 → `claude "$(bridge next)"` launches an empty session.*
- [ ] `bridge next` output is exactly the prompt: no trailing banner, no ANSI, no log line.
      *Mutation: add a "Fetched from Bridge" line → byte comparison fails.*
- [ ] The CLI imports no database module. Assert structurally, the way
      `test_no_module_outside_store_touches_the_raw_connection` does.

---

### Task 4: The `/handoff` slash command

**Files:** create `~/.claude/commands/handoff.md` (tracked in-repo at `commands/handoff.md` and
installed by a documented copy step — Bridge must not write outside `~/.bridge`, and this file is
outside it, so **installation is a manual step the README states**, never an automated write).

**Steps:**
- [ ] The command instructs Claude to compose a summary and a next-session prompt from the session,
      then invoke `bridge handoff --prompt-file -` with the prompt on stdin via a heredoc.
- [ ] It passes `--session-id` from the session so the handoff links to the transcript that produced
      it, closing the loop the spec describes.
- [ ] It states explicitly that a non-zero exit is a real failure but a spool message is success.

**Tests:**
- [ ] Round-trip against a live `TestClient`-backed server on a real port: run the actual argv the
      command specifies, with a realistic multi-paragraph prompt on stdin, and assert the queued
      handoff matches byte for byte.
- [ ] Manual verification, recorded in the ledger: run `/handoff` in this repo at the end of the
      implementation session and confirm the card shows it. **The acceptance test for the phase is
      that this plan's own successor is captured by `/handoff` and not by writing a markdown file.**

---

### Task 5: Handoff on the card, with copy-to-clipboard

**Files:** modify `src/bridge/cards.py`, `src/bridge/templates/*.html`,
`src/bridge/static/*.css`; test `tests/test_cards.py`, `tests/test_api.py`

**Steps:**
- [ ] `Card` gains `handoff: Handoff | None`. A queued handoff sorts the card to the top, ahead of
      Phase 1's dirty-and-stale ordering.
- [ ] Card renders the summary, the prompt in a scrollable block, and a copy button.
- [ ] Copy uses `navigator.clipboard` with a visible non-color-only confirmation. On the
      `http://127.0.0.1` origin the Clipboard API is available; if it rejects, fall back to
      selecting the text so the affordance never dead-ends.
- [ ] The project detail page lists past handoffs with their status.

**Tests (falsification required):**
- [ ] A project with a queued handoff sorts above a dirty-and-stale project with none.
      *Mutation: drop the handoff term from the sort key.*
- [ ] The rendered prompt is HTML-escaped: a prompt containing `<script>` appears as text.
      *Mutation: mark it safe in the template → the assertion for the literal string fails.*
- [ ] A card whose project has no handoff renders unchanged and shows no empty affordance.
- [ ] Contrast check on the new button in both themes, as Phase 1 did.

---

### Task 6 (droppable): Backfill stray handoff files

**Files:** create `src/bridge/backfill.py`; modify `src/bridge/__main__.py`
(`bridge backfill --dry-run`); test `tests/test_backfill.py`

**Steps:**
- [ ] Find `HANDOFF.md` / `NEXT-SESSION.md` in known project roots. Extract a next-prompt section
      when one is clearly delimited; otherwise store the whole file as the prompt and say so.
- [ ] `--dry-run` is the default. Writing requires `--write`.
- [ ] Idempotent: keyed on a hash of `(path, content)`, so re-running creates nothing new.

**Tests:**
- [ ] Re-running `--write` twice creates one handoff per file.
- [ ] A file with no recognizable prompt section still produces a handoff, flagged as unstructured.
- [ ] Run against the **real** files on this machine, including this repo's `HANDOFF.md`, and
      record what it produced in the ledger.

---

## Test discipline for this phase

Phase 1 shipped seven tests that passed while constraining nothing, with the suite green the whole
time. The rules that caught them apply here unchanged, plus one new one learned while building path
aliasing:

1. **Falsify every load-bearing test.** Mutate the real implementation, require an observed failure,
   paste the output. "This would fail if…" was wrong about half the time.
2. **Run against the real corpus**, not only fixtures. Three Phase 1 bugs were reachable no other
   way.
3. **Mutate the real file and `git checkout --` to restore** — a scratch copy with `PYTHONPATH` does
   not override the venv-installed package.
4. **Commit the implementation before falsifying it.** `git checkout --` restores to HEAD, so
   mutating an *uncommitted* implementation deletes it at the first restore and every later result
   is measured against a missing feature.
5. **Disable bytecode caching in the falsification harness** (`PYTHONDONTWRITEBYTECODE=1`, and clear
   `__pycache__` between runs). A mutation that only *moves* code is byte-size identical to the
   original, and `git checkout` restores it within the same second. CPython validates a `.pyc` by
   (source mtime, source size) at one-second granularity, so both match and **the stale bytecode
   compiled from the mutated source keeps executing** — in later processes, until some edit changes
   the file size. This cost an hour: it presented as a real archiving bug that reproduced
   consistently, survived a source read and `inspect.getsource` (both of which show the correct
   *file* while the wrong *bytecode* runs), and vanished only when an instrumented edit changed the
   file size. A harness is at `scratchpad/falsify.py`; fold it into the repo as
   `tools/falsify.py` before Task 1.
6. **Prefer a deterministic structural assertion to a timing-dependent one.** Phase 1's concurrency
   test catches its own bug 10 times in 20. Where an invariant can be asserted over the source
   (`test_no_module_outside_store_touches_the_raw_connection`), do that instead.

## Success criteria

- [ ] `/handoff` at the end of a real session in any project puts the prompt on that project's card.
- [ ] With the server stopped, the same `/handoff` exits zero, and the handoff appears when the
      panel is next started.
- [ ] `claude "$(bridge next)"` in a project with a queued handoff opens a session on that prompt.
- [ ] `rm ~/.bridge/bridge.db && bridge index` loses no handoff.
- [ ] A prompt with quotes, newlines, and `$(...)` survives the round trip byte for byte.
- [ ] Every load-bearing test has a recorded mutation and pasted failure output.
- [ ] `HANDOFF.md` is deleted, because its successor lives in Bridge.
