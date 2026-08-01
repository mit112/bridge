# Bridge Phase 3 — Launcher Implementation Plan

> **STATUS 2026-08-01: Phase 3 shipped and is merged.** All seven tasks below are
> implemented. Merged to `main` at `bc74b21` with 297 tests and the mutations in
> `tools/mutations/phase3-task1.json` … `phase3-task7.json`, all caught. The
> checkboxes are ticked to match.
>
> Two of those mutation specs were re-anchored afterwards (`332e9b4`, and again in
> the sweep at `72ff20f`) because later branches moved the source text they quoted;
> the behaviour they pin is unchanged.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`)
> syntax for tracking.

**Goal:** Press ▶ on a card and the queued prompt opens as a real Claude Code session — in a new
Terminal window or in the background — with a chosen model and effort, edited first if you want, and
followed back into its own transcript. This closes the loop the spec describes: queued prompt →
running session → completed session → next handoff.

**Architecture:** A new `launcher` module splits in two, and the split is the design. Command
*construction* is pure functions from arguments to strings, which is where every escaping decision
lives and where falsification pays. *Spawning* writes the prompt to a file under `~/.bridge/launches/`,
records a `launches` row with a pre-assigned session UUID before it spawns anything, then shells out
— `osascript` for a Terminal window, a plain argv `Popen` for `--bg`. The FastAPI process stays the
sole writer and becomes the sole spawner; the card and the CLI are both thin clients of
`POST /api/launch`.

**Spec:** `docs/superpowers/specs/2026-07-31-bridge-control-panel-design.md` §7 (`launcher`), §4
(`launches` schema), §6 (`POST /api/launch`, `PATCH /api/handoff`), "Data flow: the handoff loop"
steps 4–6, and the `launcher` row of the testing table.

**Builds on:** Phase 2, merged to `main` at `0092a90` (165 tests). The `handoffs` table, the
`~/.bridge/spool` outbox-and-journal, and the queued-handoff card block are in place. Phase 3
consumes what Phase 2 captures, which is what forces the journal question below.

---

## Decisions taken without an answer

Phase 2 recorded four decisions that had timed out unanswered. These five were taken the same way,
under a standing instruction to state an assumption and keep moving rather than block. Each is cheap
to reverse; **confirm before Task 3 starts**, because Tasks 3–6 all rest on the first two.

| # | Decision | Taken | Why | Reversal cost |
|---|---|---|---|---|
| 1 | How the prompt reaches `claude` in Terminal mode | **A prompt file plus `"$(/bin/cat '<file>')"`.** The prompt never crosses the AppleScript or shell quoting layers | Measured: the naive inline form is a full RCE on this machine, and the file form round-trips 483 hostile bytes exactly. Evidence in the section below | Rewrite one pure function; the pure/impure split keeps it local |
| 2 | Whether Phase 3 journals handoff *consumption* | **Yes.** `spool.journal_status` appends a status record; `rebuild_if_empty` replays creations then statuses | Phase 2 journalled creations only, noting "if it starts to bite, journal consumption too." Phase 3 is the consumer, so this is where it bites: launch a prompt, lose the database, re-index, and the panel tells you to redo work you already did | One function plus a replay loop |
| 3 | Inline-edit semantics | **Persisted.** `PATCH /api/handoff/{id}` accepts `next_prompt`, updates the row, re-journals it. Every launch separately records the exact launched bytes in `launches.prompt` | Satisfies the spec's "PATCH — edit prompt / dismiss" while keeping the journal's text current. Provenance is free from `launches.prompt`, so a later edit cannot erase what actually ran | Additive |
| 4 | `bridge launch` on the CLI | **Included, and it does *not* spool when the panel is down** | Unlike `bridge handoff`, a failed launch loses nothing — the user can run `claude` themselves. A spooled launch would fire at an unpredictable later time, which is worse than not firing at all. So: exit non-zero with a clear message | Delete one subcommand |
| 5 | How the browser writes | **`fetch()` posting JSON to the existing Pydantic API**, not an HTML `<form>` | Every route in `api.py` is JSON via Pydantic; there is no `<form>`, no HTMX, no `python-multipart`, no CSRF or CORS middleware. A form route would need all of that and would break the symmetry | Rewrite one JS file |
| 6 | How a `--bg` launch is correlated, given that `--bg` ignores `--session-id` | **Post-hoc, from the `short` handle `--bg` prints**, resolved to a full UUID via `claude agents --json --all`. Pre-assignment is kept for terminal mode only | Forced, not chosen. `claude --bg` mints its own session id and emits `warning: --bg manages the session id; ignoring --session-id`. The two modes therefore have genuinely different correlation stories, and pretending otherwise would silently break background launches | Contained to one function plus a nullable column |

---

## Global Constraints

Phase 1's and Phase 2's constraints all carry forward. Restated where Phase 3 can violate them, plus
the ones this phase introduces:

- **Bridge never writes to a user project repo.** Phase 3 adds exactly one writable location,
  `~/.bridge/launches/`, mode `0700`. Everything still lives under `~/.bridge`.
- **Bridge launches sessions; it never hosts or supervises them.** After a successful spawn the
  launcher's job is over. It does not wait on, poll, or kill a session.
- **The prompt is never interpolated into a shell string.** In background mode it is a single argv
  element. In terminal mode it is not on the command line at all.
- **Every variable field that does reach the shell is POSIX single-quoted**: the project path, the
  prompt-file path, the session title, the model, and the effort. Single-quoting is not optional
  styling — `do script` runs an **interactive** zsh, so history expansion is live and a
  double-quoted `!!` in a title is RCE-adjacent. A `zsh -c` test would not catch that.
- **A `launches` row is written before the spawn**, so a session is correlatable even when the spawn
  fails. This is a spec requirement, not an optimisation.
- **A failed launch never consumes its handoff.** The prompt stays queued, and the UI both reports
  the error and copies the prompt, so the user is never stuck.
- **`claude` is resolved through an injected `which`, never hardcoded.** `gitprobe` hardcodes
  `GIT = "/usr/bin/git"`, which is correct there and would be fatal here: a hardcoded path makes a
  fake-`claude`-on-`PATH` test pass vacuously while testing nothing. `resolve_claude(which=...)`
  is what keeps the launcher's tests honest.
- **Nothing in the test suite ever spawns a real Claude session.** A fake `claude` that records argv
  is the entire verification surface. A real session burns tokens, writes a transcript the indexer
  then ingests, and is not repeatable.
- Migrations remain **additive only**. `launches` is a new table; no existing table is rebuilt.
- Absolute coreutil paths in every shell-out. Bind `127.0.0.1` only.
- WCAG 2.2 AA on the new controls: visible focus ring and accessible name on every select, textarea
  and button; launch result never conveyed by colour alone; the whole band keyboard-operable.

---

## The prompt crosses two quoting layers, so it is taken off the command line

This is Phase 3's central hazard and the reason Task 2 exists as a task of its own.

Terminal mode is, in the spec's words, "`osascript` opens a new Terminal window running
`cd <path> && claude …`". Concretely that is a shell command nested inside an AppleScript string
literal inside an `osascript -e` argument, and a prompt placed there is parsed by AppleScript *and*
by an interactive zsh. Phase 2 already shipped this bug once in a single-layer form: `/handoff`
passed `--summary "<text>"`, and a summary containing `$(...)` was **executed**.

This was measured before the plan was written, not reasoned about. The naive inline form:

```
sent:     claude --session-id … "INLINE $(echo PWNED) `echo BACKTICK_EXECUTED` ${HOME} end"
received: b'INLINE PWNED BACKTICK_EXECUTED /Users/mitsheth end'
```

Full remote-code-execution equivalent, both substitutions evaluated. The prompt-file form, over the
same transport, with a 483-byte hostile fixture and a project directory named
`proj dir's "na\me"`:

```
byte_exact_after_trailing_nl_strip: TRUE    first_diff_index: None
$(echo PWNED) literal: TRUE    `echo …` literal: TRUE    ${HOME} literal: TRUE
PWNED leaked: FALSE            BACKTICK_EXECUTED leaked: FALSE
```

Leading indentation, trailing spaces before the final newline, all nine newlines, emoji and CJK all
survived byte-identical. Command substitution output inside double quotes is not re-scanned for
metacharacters — confirmed empirically, not assumed.

So the shape is:

```
[ -r '<promptfile>' ] || { echo 'bridge: prompt file missing' >&2; exit 1; }; \
cd '<proj>' && '<claude>' --session-id <uuid> --model '<m>' --effort '<e>' \
    -n '<title>' "$(/bin/cat '<promptfile>')"
```

Six consequences, every one of them found by the probe and every one of them asserted rather than
glossed:

- **The `[ -r ... ]` guard is load-bearing, not defensive noise.** Without it a missing or empty
  prompt file makes `cat` write to stderr, leaves the shell's exit status at 0, and launches
  `claude` **with an empty prompt** — observed as `argc=4` with a final `b''`. A silent empty
  session is the worst failure available here, because it looks like it worked.
- **Do not delete the prompt file when `osascript` returns.** `do script` returns immediately and
  `cat` runs later, in the new shell. Eager deletion is a live race that produces exactly the empty
  prompt above. Retain the file; garbage-collect by age.
- **Trailing newlines are asymmetric.** `$(cat)` strips them; the `--bg` argv path preserves them.
  Normalise with `prompt.rstrip("\n")` at file-write time so both modes are byte-identical, rather
  than documenting a discrepancy.
- **NUL truncates.** `before\x00after` arrives as `b'before'`. Unavoidable through argv, so reject
  NUL at the boundary.
- **`ARG_MAX` is 1,048,576 and the file does not lift it** — the substitution still becomes argv.
  Measured: 900 KiB works, 1024 KiB fails with `argument list too long`, rc 127. Cap well below.
- **A newline in the project path is unrepresentable** in an AppleScript literal, and is legal in
  APFS. The escaper raises rather than silently corrupting.

Background mode has no shell and no AppleScript: `Popen([claude, "--bg", …, prompt])` passes the
prompt as one argv element, verified byte-exact at 483 bytes. It is deliberately *not* routed
through the prompt file, so the two modes fail independently.

One new risk is accepted in exchange for removing the RCE: the prompt is now at rest on disk.
`~/.bridge/launches/` is `0700` and each file is `0600`.

## `--bg` ignores `--session-id`, so the two modes correlate differently

The spec says the launcher generates the session UUID and that "because the UUID is pre-assigned, the
indexer links the launch to its transcript on the next scan." That is true of terminal mode and
**false of background mode**, which was established by reading `claude` 2.1.220's dispatch path
rather than by assuming the flag composes:

```
warning: --bg manages the session id; ignoring --session-id (use --resume <id> to continue an
existing session)
```

`--bg` mints its own UUID and passes *that* to the worker. A pre-assigned id is discarded with a
stderr warning and nothing else, so a launcher that recorded it would hold a row whose
`session_id` matches no transcript that will ever exist — a correlation that fails silently and looks
like a session that never started.

What `--bg` does give back is a parseable handle on stdout:

```
backgrounded · <short>[ · <name>]
```

where `short` is `/^[a-f0-9]{8}$/` and is exactly `session_id[:8]`. Confirmed against live
`claude agents --json --all`: `"id": "00b31445"` alongside
`"sessionId": "00b31445-a2d0-4d3b-878b-e37f81284385"`. So background correlation is: capture stdout,
strip ANSI (the handle is wrapped in a colour escape and whether chalk disables on a pipe was **not**
confirmed, so strip defensively), match the handle, then resolve the full UUID from
`claude agents --json --all`. Falling back to globbing
`~/.claude/projects/<enc-cwd>/<short>*.jsonl` is acceptable and needs no subprocess.

Three further constraints from the same source, each of which would otherwise surface as a confusing
runtime failure:

- **A pre-assigned UUID must be unused, and "unused" is per-project-dir**, checked by `statSync` on
  `<cwd-project-dir>/<uuid>.jsonl`. A collision exits 1 with `Error: Session ID … is already in use.`
  before anything starts. Treat that as *retry with a new UUID*, not as a launch failure.
- **Generate lowercase.** The format gate is case-insensitive and does **not** normalise, while APFS
  is case-insensitive: an uppercase UUID passes validation, then collides with a lowercase
  transcript, and the error echoes your input verbatim. Lowercase keeps the recorded id and the
  filename identical.
- **`-n/--name` has no length or character validation and is injected into the terminal-title escape
  sequence.** Sanitise control characters and newlines out of the title ourselves; nothing downstream
  will.

Also confirmed and worth stating because it closes off a tempting alternative: there is **no flag
that reads the user prompt from a file.** `--system-prompt-file` and `--append-system-prompt-file`
exist but set the *system* prompt. The user prompt must come from argv or stdin, which is what makes
Task 2's prompt-file-plus-`$(cat)` construction necessary rather than merely convenient.

## Consuming a handoff is a status change, and Phase 2 does not journal those

Phase 2's journal records creations. `spool.rebuild_if_empty` replays them into an empty `handoffs`
table, guarded so a routine index cannot resurrect a consumed prompt. Its own docstring names the
gap: "Phase 2 journals creations and not status changes, so a rebuilt table shows every recorded
handoff as queued again. That is the right trade for recovering a wiped cache and the wrong one for
routine use."

Phase 3 makes that trade wrong. Before Phase 3 nothing consumed a handoff, so "everything comes back
queued" cost nothing. Once ▶ exists, *launch → `rm ~/.bridge/bridge.db` → `bridge index`* puts a
prompt you already ran back at the top of the dashboard, and the panel's most load-bearing signal
starts lying.

Task 1 closes it: `spool.journal_status` writes a status record into `drained/`, and
`rebuild_if_empty` applies every creation and then every status record in timestamp order. The
empty-table guard stays exactly as it is — this makes recovery *faithful*; it does not make replay
routine. Any design that replays statuses on a non-empty table forfeits the guard and must be
rejected in review.

---

### Task 1: `launches` table, store methods, and journalled status changes

**Files:** modify `src/bridge/store.py`, `src/bridge/spool.py`, `src/bridge/models.py`;
test `tests/test_store.py`, `tests/test_spool.py`

**Interfaces:**
- Produces `models.Launch` with `id`, `project_id`, `handoff_id`, `session_id`, `mode`, `model`,
  `effort`, `prompt`, `launched_at`, `outcome`.
- Produces `Store.create_launch(l: Launch) -> str`, `Store.set_launch_outcome(id, outcome)`,
  `Store.launches(project_id, limit) -> list[Row]`,
  `Store.launch_by_session(session_id) -> Row | None`.
- Produces `spool.journal_status(handoff_id, status, at, spool_dir) -> Path`.
- Schema (additive): `launches(id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES
  projects(id), handoff_id TEXT REFERENCES handoffs(id), session_id TEXT, short_id TEXT, mode TEXT
  NOT NULL, model TEXT, effort TEXT, prompt TEXT NOT NULL, launched_at INTEGER NOT NULL, outcome
  TEXT NOT NULL DEFAULT 'pending')`, with `mode ∈ {terminal, background}` and
  `outcome ∈ {pending, started, failed}`, plus
  `CREATE INDEX idx_launches_project ON launches(project_id, launched_at)` and
  `CREATE INDEX idx_launches_session ON launches(session_id)`.
- Produces `Store.set_launch_session(id, session_id, short_id)`, for the background mode that cannot
  know its session id until after the spawn.

**Steps:**
- [x] Append the table and both indexes to `SCHEMA`. `handoff_id` is nullable — a launch may carry
      an ad-hoc prompt with no handoff behind it.
- [x] `session_id` is **nullable**, and this is forced rather than chosen. `claude --bg` *ignores*
      `--session-id` and mints its own (see the section below), so a background launch has no session
      id at the moment its row is written. `short_id` holds the 8-hex handle `--bg` prints, which is
      exactly `session_id[:8]`, and `set_launch_session` fills both in once they are known. A
      `NOT NULL session_id` would force a placeholder, and a placeholder in a correlation key is how
      you get a launch that joins to the wrong session.
- [x] `spool.journal_status` writes through the same `_atomic_write` temp-file-plus-`os.replace`
      path as `journal`, into `drained/`, under a name that cannot collide with a handoff record
      (`<handoff-id>.<epoch>.status.json`).
- [x] `rebuild_if_empty` loads creation records and status records separately, applies every
      creation, then applies status records ordered by their `at`, so a superseded-then-consumed
      history replays to the same end state it had before the loss.
- [x] Add `# --- Phase 3: the launcher ---` banners to the touched test modules, matching the
      existing `# --- Phase 2: handoff routes ---` convention.

**Tests (each requires an observed failure from a mutation):**
- [x] `create_launch` then `set_launch_outcome('started')` round-trips, and `launch_by_session`
      finds the row by its pre-assigned session id.
      *Mutation: drop the `WHERE session_id=?` clause → the wrong row returns.*
- [x] A launch with `handoff_id=None` inserts.
      *Mutation: declare the column `NOT NULL` → the insert raises.*
- [x] A launch referencing a handoff that does not exist is refused by the foreign key.
      *Mutation: remove `REFERENCES handoffs(id)` → the bad insert succeeds.*
- [x] After `create_handoff` plus `journal_status(id, 'consumed')`, wiping the table and calling
      `rebuild_if_empty` yields that handoff as **consumed**, and `queued_handoff` returns `None`.
      *Mutation: skip the status-replay loop → the handoff comes back queued.* This is the exact
      regression the task exists to prevent; it is the one mutation that must not survive.
- [x] Status records replay in `at` order — queued → superseded → consumed ends consumed — even when
      the files are globbed out of order.
      *Mutation: drop the sort → the end state becomes filesystem-dependent.*
- [x] `rebuild_if_empty` is still skipped on a non-empty table with status records present.
      *Mutation: remove the `handoff_count() > 0` guard → a routine index rewrites live statuses.*
- [x] A corrupt status record lands in `bad/` and does not block the rest of the replay.

---

### Task 2: `launcher` command construction — pure functions, all the escaping

**Files:** create `src/bridge/launcher.py`; test `tests/test_launcher.py`

This task writes no files, spawns no processes, and opens no database. Everything in it is a pure
function from arguments to a string, which is what makes the escaping exhaustively testable and what
makes the mutations below cheap to run.

**Interfaces:**
- Produces `launcher.sh_quote(s) -> str` — POSIX single-quote quoting, `'` → `'\''`.
- Produces `launcher.as_quote(s) -> str` — AppleScript literal quoting; escapes `\` **then** `"`,
  and raises `LaunchError` on a newline or carriage return, which an AppleScript literal cannot hold.
- Produces `launcher.resolve_claude(which=shutil.which) -> str`.
- Produces `launcher.build_shell_command(spec, prompt_path) -> str` and
  `launcher.build_applescript(command) -> str` and `launcher.build_bg_argv(spec) -> list[str]`.
- Produces `launcher.LaunchSpec` with `project_path`, `prompt`, `session_id`, `model`, `effort`,
  `title`, `mode`; `launcher.LaunchError`; `launcher.MAX_PROMPT_BYTES`.

**Steps:**
- [x] `sh_quote` is applied to the project path, the resolved `claude` path, the prompt-file path,
      the title, the model, and the effort. Model and effort are passed through unvalidated per the
      spec — the CLI is the authority on what is accepted — which is exactly why they must be quoted.
- [x] `as_quote` escapes backslash **before** double quote. The other order double-escapes and is the
      classic way to get this wrong. `$`, backtick and `!` are not special to AppleScript and must
      not be touched; they are handled by the shell layer's single quotes.
- [x] `build_shell_command` emits the `[ -r … ] || { …; exit 1; }` guard first, then
      `cd`, then `--session-id`, `--model`, `--effort`, `-n`, and finally
      `"$(/bin/cat '<path>')"`. `--model`, `--effort` and `-n` are omitted entirely when their value
      is `None`, never passed as an empty string.
- [x] `build_bg_argv` **omits `--session-id` entirely.** `--bg` ignores it and warns, so passing it
      is noise that also implies a correlation the code does not have. Keep `-n/--name`: it survives
      into both the stdout line and `claude agents --json`, and is the best human-readable tie-back.
- [x] The title defaults to the handoff summary's first line, truncated, falling back to the project
      name. It is authored text, so it goes through `sh_quote` like everything else — and because
      `claude` applies no validation and injects it into the terminal-title escape sequence, control
      characters and newlines are stripped from it first.
- [x] Session UUIDs are generated **lowercase**. `claude`'s format check is case-insensitive and does
      not normalise, and APFS is case-insensitive, so an uppercase id would collide with a lowercase
      transcript while the recorded id and the filename disagreed.
- [x] Reject a prompt containing NUL, and a prompt over `MAX_PROMPT_BYTES`, with a `LaunchError`
      naming the size and the limit. Set the cap at 800 KiB: 900 KiB was measured working and
      1024 KiB measured failing with `argument list too long`, so this leaves real headroom.

**Tests (falsification required):**
- [x] `build_shell_command` does **not** contain the prompt text, for a prompt containing
      `$(echo nope)`. `assert prompt_fragment not in command`.
      *Mutation: interpolate the prompt directly → the assertion fires.* This is Phase 2's
      `--summary` bug in its Phase 3 form and the single most important test in the phase.
- [x] `sh_quote` round-trips through a real shell: `/bin/sh -c "printf %s " + sh_quote(s)` returns
      `s` byte for byte. Table-driven over quote, double quote, backtick, `$(...)`, `${HOME}`,
      backslash, newline, `;`, `!`, `!!`, emoji, CJK, and the empty string.
      *Mutation: use the naive `s.replace("'", "\\'")` → the round trip breaks.*
- [x] `as_quote` escapes both `\` and `"`, does not double-escape, and leaves `$`/backtick/`!`
      untouched. *Mutation: swap the two replacement lines → the backslash is escaped twice.*
- [x] `as_quote` raises on a project path containing a newline rather than silently stripping it.
- [x] The command contains the `[ -r` guard. *Mutation: drop the guard → the assertion fires*, and
      Task 3's empty-prompt test then also goes red, which is the pair that makes this real.
- [x] `build_bg_argv` puts the prompt in exactly one element and includes `--bg`.
      *Mutation: join the argv into a single string → the element-count assertion fires.*
- [x] `build_bg_argv` contains **no** `--session-id`. `assert "--session-id" not in argv`.
      *Mutation: add it → the assertion fires.* Without this test the flag gets added back by anyone
      who reads the spec's pre-assignment paragraph and not this plan.
- [x] Generated session ids are lowercase and match `claude`'s 8-4-4-4-12 hex validator.
      *Mutation: emit `str(uuid4()).upper()` → the lowercase assertion fires.*
- [x] A title containing a newline, a `\x1b`, and a `\x07` is sanitised before quoting.
      *Mutation: skip the sanitiser → the raw control bytes appear in the command.*
- [x] `--model` and `--effort` are absent from argv when `None`, not present-and-empty.
      *Mutation: always emit them → an empty `--model ''` appears.*
- [x] `resolve_claude` raises `LaunchError` when the injected `which` returns `None`.
- [x] A prompt over the cap, and a prompt containing NUL, each raise before anything is constructed.

---

### Task 3: `launcher` spawn, prompt file, and outcome recording

**Files:** modify `src/bridge/launcher.py`, `src/bridge/config.py`, `tests/conftest.py`;
test `tests/test_launcher.py`

**Interfaces:** produces `launcher.launch(store, cfg, spec, handoff_id=None) -> LaunchResult`
carrying `session_id`, `launch_id`, `outcome`, and `error: str | None`. Consumes Task 1's store
methods and Task 2's pure builders.

**Steps:**
- [x] The order is fixed and load-bearing: resolve `claude` → write the prompt file → insert the
      `launches` row with `outcome='pending'` → spawn → record the outcome. Anything failing before
      the row exists fails with no side effects; anything after it is correlatable.
- [x] The prompt file is `cfg.launches_dir / f"{session_id}.prompt"`, written through the same atomic
      temp-file-plus-`os.replace` idiom `spool` uses, directory `0700` and file `0600`, with the
      prompt `rstrip("\n")`-normalised so terminal and background modes agree byte for byte.
- [x] Terminal mode runs `["/usr/bin/osascript", "-e", script]` as an argv list — the script is one
      element, never a shell string. Background mode runs `build_bg_argv` with no shell.
- [x] The prompt file is retained after a successful launch. It is provenance, it is literally what
      ran, and deleting it eagerly is a race against the new shell's `cat`. Add age-based GC, not
      launch-time deletion.
      **[2026-08-01: half true.** Retention shipped and is correct. `launcher.gc_prompt_files` was
      written and is unit-tested, but **nothing in production ever calls it** — not `api.py`,
      `cli.py`, `__main__.py` or `indexer.py` — so `~/.bridge/launches/*.prompt` currently grows
      without bound. The function exists; the GC does not run.]
- [x] Terminal mode pre-assigns the session id, and a `Session ID … is already in use.` exit is a
      **retry with a fresh UUID**, bounded to a small number of attempts, not a launch failure. The
      check is per-project-dir, so a collision is rare but entirely possible.
- [x] Background mode writes its row with `session_id=NULL`, then parses the spawn's stdout for
      `backgrounded · <short>`, **stripping ANSI first**, and calls `set_launch_session`. Resolving
      `short` to the full UUID via `claude agents --json --all` is best-effort: if it fails, keep
      `short_id` alone and let Task 7's glob close the loop later. A background launch whose handle
      could not be parsed is still `started` — it did start — with both ids null and a note.
- [x] On success, `outcome='started'` and, when a `handoff_id` was supplied, the handoff is marked
      `consumed` **and** that status is journalled via Task 1.
- [x] On failure, `outcome='failed'`, the error text lands on the result, and the handoff is left
      `queued`. The launcher's contract is only that it does not consume; the clipboard fallback
      belongs to the caller.
- [x] `Config` gains `launches_dir`, defaulting to `~/.bridge/launches` and overridable exactly like
      `spool_dir`. `tests/conftest.py`'s autouse guard grows to cover it: a launch writes files, and
      a fixture that forgets the override would litter the real directory. Extend the existing
      `never_touch_the_real_bridge_dir` guard rather than adding a second one — it already raises
      `RealBridgeDirTouched(BaseException)` so no catch-all can swallow it.

**Tests (falsification required):**
- [x] Background mode against a **fake `claude` on `PATH`** that dumps its argv: the prompt arrives
      byte for byte including quotes, backticks, `$(...)`, newlines, emoji and CJK. There is no
      existing fake-executable fixture in this suite, so this is new ground — write the shim, `chmod`
      it `0o755`, and prepend its directory to `PATH` the way `test_handoff_command.py` prepends the
      venv's `bin`. *Mutation: pass the prompt through `shlex.quote` as well → the bytes differ.*
- [x] The shell layer of terminal mode, run through `/bin/sh -c` with the same fake `claude` — no
      AppleScript, no Terminal window. Prompt matches byte for byte after the `rstrip` normalisation,
      and `$(echo nope)` arrives literal.
      *Mutation: drop the double quotes around `$(/bin/cat …)` → word splitting shatters the prompt
      into many argv elements.*
- [x] A **missing prompt file** produces a non-zero shell exit and **no `claude` invocation at all**
      — assert the fake recorded nothing. *Mutation: remove the `[ -r … ]` guard → `claude` runs
      with an empty prompt, which is the observed silent-success failure this guard exists for.*
- [x] A failed spawn records `outcome='failed'` and leaves the handoff `queued`.
      *Mutation: consume the handoff before checking the spawn result → the prompt is lost on
      failure, the worst outcome available in this phase.*
- [x] A `launches` row exists even when the spawn fails.
      *Mutation: move the insert after the spawn → no row exists.*
- [x] The prompt file still exists after a successful launch.
      *Mutation: unlink it after spawning → the assertion fires.*
- [x] A successful launch marks the handoff `consumed`, stamps `consumed_at`, and leaves a status
      record in `drained/`.
- [x] A background launch whose fake `claude` prints `backgrounded · \x1b[36mdeadbeef\x1b[0m` yields
      `short_id='deadbeef'`. *Mutation: skip the ANSI strip → the handle keeps its escape bytes and
      no glob or lookup will ever match it.*
- [x] A background launch whose stdout is unparseable is still `outcome='started'` with both ids
      null. *Mutation: mark it `failed` → the handoff is left queued for a session that is running.*
- [x] A terminal launch that collides on session id retries with a fresh UUID and succeeds; the retry
      is bounded. *Mutation: treat the collision as fatal → the launch fails for a recoverable
      reason.*
- [x] `resolve_claude` genuinely consults `PATH`: point the injected `which` at a directory with no
      `claude` and assert `LaunchError`.
      *Mutation: hardcode `/Users/mitsheth/.local/bin/claude` → the fake-on-PATH tests above pass
      vacuously while testing nothing.* This mutation is the reason the constraint exists.
- [x] The conftest guard fires when a launcher test omits the `launches_dir` override.

---

### Task 4: `POST /api/launch` and editable prompts

**Files:** modify `src/bridge/api.py`; test `tests/test_api.py`

**Interfaces:** produces `POST /api/launch` (body: `project_path`, `prompt`, `mode`, `model`,
`effort`, `handoff_id`, `title`) → `200 {session_id, launch_id, outcome, error}`; extends
`PATCH /api/handoff/{id}` to accept `next_prompt` alongside `status`.

**Steps:**
- [x] The route resolves `project_path` through the alias table exactly as `POST /api/handoff` does,
      so a launch from an old `~/Documents/...` path attaches to the canonical project.
- [x] A launch failure is **not** an HTTP error. It returns `200` with `outcome='failed'` and an
      `error` string, because the UI needs the error text and the prompt in the same response to
      offer the clipboard fallback. A 500 would give it neither.
- [x] `PATCH` accepts `next_prompt`, `status`, or both. A `next_prompt` change updates the row and
      re-journals the handoff so the journal's text stays current; a `status` change journals the
      status via Task 1. A `PATCH` with neither field is `422`, not a silent no-op.
- [x] The launcher is injected into `create_app` with a default, so a test can substitute a recording
      double without monkeypatching a module global — and so no test can spawn anything by accident.

**Tests (falsification required):**
- [x] A launch from an alias path attaches to the canonical project.
      *Mutation: skip the alias resolution → it attaches to the old path and splits history again.*
- [x] A failed launch returns `200` with `outcome='failed'` and a non-empty `error`.
      *Mutation: raise `HTTPException(500)` → the response body carries no prompt to copy.*
- [x] `PATCH next_prompt` changes what `GET /api/handoff` returns and leaves the status `queued`.
      *Mutation: ignore `next_prompt` → the old text comes back.*
- [x] A prompt with quotes, backticks, `$(...)`, newlines and a 40 KB body round-trips byte for byte
      through `PATCH` → DB → `GET`, mirroring the Phase 2 assertion for `POST`.
- [x] `PATCH` with an empty body is `422`.
- [x] A successful launch consumes the handoff, so the next `GET /api/handoff` for that project is
      `204`. *Mutation: leave the status queued → the card keeps offering a prompt already running.*

---

### Task 5: `bridge launch`

**Files:** modify `src/bridge/cli.py`; test `tests/test_cli.py`

**Interfaces:**
```
bridge launch [--project P] [--mode terminal|background] [--model M] [--effort E]
              [--prompt-file -]        # default: the project's queued handoff
```

**Steps:**
- [x] With no `--prompt-file`, the CLI sends no prompt and the **server** uses the queued handoff, so
      the prompt is never round-tripped through the client for no reason. `--mode` defaults to
      `terminal`.
- [x] Panel down, timeout, or 5xx → a clear stderr message and **exit 1**. No spooling. Unlike
      `handoff`, nothing is lost, and a launch that fires at an unpredictable later time is worse
      than one that never fires. State this asymmetry in the docstring, because it reads like an
      inconsistency until you see the reason.
- [x] Nothing queued and no `--prompt-file` → exit 1 with a message, matching `bridge next`.
- [x] The CLI still imports no database module, and still does not import `launcher` — the server
      spawns, not the client.

**Tests (falsification required):**
- [x] Against a genuinely closed port (`closed_port()`, verified with `connect_ex`, as
      `test_cli.py` already does), `bridge launch` exits **1** — the opposite of `bridge handoff`.
      *Mutation: return 0 → a silent non-launch reports success.*
- [x] It writes no spool file. *Mutation: spool on failure → a stray journal file appears.*
- [x] Against the existing `fake_server` fixture, the POST body carries the right project, mode,
      model and effort.
- [x] `--prompt-file -` overrides the queued handoff.
- [x] `test_the_cli_never_loads_a_database_module` is extended to assert `bridge.launcher` is absent
      from `sys.modules` too, using the same subprocess-observation idiom.

---

### Task 6: The launch band on the card

**Files:** modify `src/bridge/cards.py`, `src/bridge/templates/_card.html`,
`src/bridge/templates/project.html`, `src/bridge/static/app.css`,
`src/bridge/static/copy.js`, `src/bridge/config.py`; create `src/bridge/static/launch.js`;
test `tests/test_cards.py`, `tests/test_api.py`, `tests/test_contrast.py`

**REQUIRED:** invoke the `design-guardrails` skill before writing the CSS, per the standing
repository rule for interface work and exactly as Phase 1's Task 9 did.

This is the first form UI in the codebase. `app.css` contains no `select`, `input`, `textarea`,
`label` or `::placeholder` rule at all, and no template contains a `<form>`. That is why the
tokens below are new work rather than reuse.

**Steps:**
- [x] The launch band renders **outside** the `{% if card.handoff %}` guard, between the handoff
      section and `card__burn` — a project with nothing queued is still launchable. Consequence:
      element ids must be prefixed from `card.project_id`, not from Phase 2's
      `hid = "handoff-" ~ card.handoff.id`, which does not exist on a card with no handoff. Getting
      this wrong collides ids across cards and silently breaks every `<label for>` and
      `aria-labelledby`.
- [x] The two selects default from `card.handoff.suggested_model` and `.suggested_effort`, which
      `cards._handoff` already passes to the template as a plain dict and which nothing currently
      reads. No backend change is needed for the defaults. Fall back to the first configured value.
- [x] `config.DEFAULT_EFFORTS` grows `xhigh` and `max`, because `claude --effort` accepts
      `low, medium, high, xhigh, max` and Phase 3 is the first phase to surface the list. The spec's
      `~/.bridge/config.toml` stays out of scope; the defaults stay in code.
- [x] The prompt becomes a `<textarea>` with an accessible name, saved by `PATCH` on blur only when
      the text changed. Inherit `.handoff__prompt`'s `--mono`, `.78rem`, `1.45` line-height,
      `var(--card)` background and `4px` radius, so the editable field reads as the same object as
      the `<pre>` it replaces.
- [x] **Fix `copy.js` while swapping the `<pre>` for a `<textarea>`.** It reads
      `source.textContent`, which on a textarea returns the server-rendered text and *not* the user's
      edits, so Copy would silently hand over a stale prompt. Read `.value` when present, and prefer
      `select()` over `createRange()` in the fallback for a form control.
- [x] The launch status uses a `role="status"` live region with a **distinct** `data-*` key from the
      copy status. `copy.js` targets `[data-copy-status="${id}"]`; reusing the id would let the two
      overwrite each other's messages.
- [x] On failure the message is glyph **plus** words and the prompt is copied automatically — e.g.
      `⚠ Launch failed — prompt copied, paste it in your terminal`. Never a red border alone. This
      is what satisfies the spec's "surface error **and** copy prompt to clipboard".
- [x] `launch.js` reuses `copy.js`'s clipboard helper rather than re-implementing it; extract the
      shared function if that means editing `copy.js`.
- [x] Add a `--field-line` colour token for form-control borders. **`--line` cannot be reused**: it
      measures 1.34:1 light and 1.28:1 dark against `--card`, and WCAG 1.4.11 requires 3:1 for a
      control's visible boundary. Define it as **6-digit lowercase hex** — `test_contrast.py` parses
      with `(--[a-z-]+):\s*(#[0-9a-fA-F]{6})`, so an `oklch()`, `#abc`, or `rgb()` value is
      silently invisible to the contrast suite and would ship unchecked.
- [x] Add a `.btn:disabled` state — none exists — for the in-flight ▶, and give an icon-only button
      an explicit `min-width` to hold the 24×24 target `min-height: 1.75rem` already guards.
- [x] The project detail page lists launch history: mode, model, effort, outcome, and the linked
      session when one exists.

**Tests (falsification required):**
- [x] The rendered textarea is HTML-escaped: a prompt containing `</textarea><script>` appears as
      text. *Mutation: mark it safe in the template → the literal-string assertion fails.* A
      textarea escapes differently from a `<pre>`, so Phase 2's equivalent test does not cover it.
- [x] Both selects carry an accessible name, and the suggested value is pre-selected.
      *Mutation: drop the `selected` attribute → the assertion fires.*
- [x] Two cards on one dashboard produce no duplicate element id.
      *Mutation: key the ids off the handoff id → a card with no handoff emits a bare prefix and the
      ids collide.*
- [x] A card with no queued handoff still renders a launch band, and renders no empty prompt block.
- [x] `--field-line` clears 3:1 against `--card` in **both** themes, by adding a row to
      `test_contrast.py`'s `PAIRS` table rather than writing a second checker. The Phase 2 border
      token failed at 2.18:1 dark and 2.9:1 light and was recomputed; the new token gets the same
      treatment, not an eyeball.
- [x] Keyboard operability asserted structurally: no `tabindex="-1"` on any new control, and every
      new control has a `<label for>` or an `aria-label`.

---

### Task 7: Correlating a launch back to its transcript

**Files:** modify `src/bridge/indexer.py`, `src/bridge/templates/project.html`;
test `tests/test_indexer.py`

**Steps:**
- [x] For **terminal** launches there is no new parsing and no indexer change. The link is
      `launches.session_id = sessions.id`, and it exists the moment the indexer writes the session,
      because the UUID was pre-assigned. The work is asserting that, not building it.
- [x] For **background** launches, add a backfill step to the index run: for any launch with
      `short_id` set and `session_id` still null, match a session whose id starts with that
      `short_id` and fill it in. Eight hex characters is 2^32, and the candidate set is one project's
      sessions, so require a **unique** prefix match and leave it null on ambiguity rather than
      guessing. This is the only place Phase 3 touches `indexer.py`.
- [x] After indexing a transcript whose filename and `sessionId` are a launched UUID,
      `launch_by_session` joins to a real session row and the detail page shows the launch and the
      session as one thing.
- [x] A launch whose session never appears — a spawn that started nothing, or a session quit before
      it wrote a transcript — stays visible as `started` with no session. Not an error; the panel
      shows what it knows.

**Tests (falsification required):**
- [x] Index a fixture transcript whose session id equals a launched `session_id`; assert the join
      finds it and the project attribution matches.
      *Mutation: have the launcher mint a fresh UUID at spawn time instead of using the pre-assigned
      one → the join finds nothing.* That is the spec requirement "the indexer links the launch to
      its transcript on the next scan" failing outright.
- [x] A background launch with only `short_id` set resolves to its full `session_id` on the next
      index. *Mutation: match on `LIKE short || '%'` without the uniqueness check → an ambiguous
      prefix silently binds the launch to the wrong session.*
- [x] Two sessions sharing a `short_id` prefix leave `session_id` null rather than picking one.
- [x] A launch with no matching session renders the detail page without error.
- [x] Run against the **real** corpus once: index, then assert no launch row joins to a session it
      did not launch.

---

## Test discipline for this phase

Phase 2's rules carry forward unchanged, and Phase 2's own results are the argument for them: three
real bugs that session were found only by falsification, never by writing a test.
`tools/falsify.py` enforces rules 1–5 mechanically, so do not hand-roll a mutation.

1. **Falsify every load-bearing test.** Mutate the real implementation, require an observed failure,
   paste the output. Mutation specs go in `tools/mutations/phase3-task*.json`, matching the existing
   43 across six tasks.
2. **Run against the real corpus**, not only fixtures.
3. **Mutate the real file and `git checkout --` to restore.**
4. **Commit the implementation before falsifying it.**
5. **Bytecode caching stays off** (`PYTHONDONTWRITEBYTECODE=1`, `__pycache__` cleared around every
   run). A mutation that only moves code is byte-size identical and stale `.pyc` outlives the
   restore.
6. **Prefer a deterministic structural assertion to a timing-dependent one.**
7. **New for Phase 3: never spawn a real session.** Every launch test goes through a fake `claude`
   that records argv. A real session burns tokens, writes a transcript the indexer then ingests, and
   is not repeatable. If a test needs a real Terminal window it is a manual verification recorded in
   the ledger, not a pytest.
8. **New for Phase 3: assert absence, not just presence.** The central safety property is that the
   prompt is *not* in the constructed command, and `assert prompt not in command` is the only shape
   that expresses it. A presence-only assertion cannot catch the Phase 2 `--summary` injection.
9. **New for Phase 3: a fake on `PATH` is only meaningful if the code consults `PATH`.** The suite
   has no fake-executable precedent, and `gitprobe`'s hardcoded `/usr/bin/git` is the trap: hardcode
   `claude` the same way and every launcher test passes while testing nothing. The `resolve_claude`
   mutation in Task 3 is what proves the fake is real.

## Success criteria

- [x] ▶ on a card with a queued handoff opens a Terminal window running that prompt with the chosen
      model and effort, and the session appears on the project's card after the next index.
- [x] The same launch in background mode starts a session that appears in `claude agents --json`, and
      its `short_id` resolves to a full `session_id` on the next index.
- [x] A prompt containing quotes, backticks, `$(...)`, newlines and emoji launches with its text
      intact and nothing executed, byte for byte, in both modes.
- [x] A missing prompt file never launches an empty session.
- [x] A failed launch leaves the handoff queued, shows the error, and puts the prompt on the
      clipboard.
- [x] Editing a queued prompt in the panel persists, and Copy hands over the edited text, not the
      text the server rendered.
- [x] Launch a handoff, then `rm ~/.bridge/bridge.db && bridge index`: it comes back **consumed**,
      not queued.
- [x] No test spawns a real Claude session.
- [x] Every load-bearing test has a recorded mutation and pasted failure output.
- [x] This plan's successor is captured by `/handoff`, launched with ▶, and the launched session is
      linked back to its transcript. That round trip is the acceptance test for the phase.

---

## Self-Review

**1. Spec coverage.** Every Phase 3 spec requirement maps to a task:

| Spec requirement | Task |
|---|---|
| `launches` schema, additive migration | 1 |
| Launcher generates the session UUID and writes the row **before** spawning | 1, 3 |
| Prompt as a single argv element, AppleScript payload escaped | 2 |
| terminal mode via `osascript`, background mode via `--bg` | 2, 3 |
| `POST /api/launch` | 4 |
| `PATCH /api/handoff/{id}` — edit prompt / dismiss | 4 |
| Prompt editable in the panel before launch | 4, 6 |
| Model and effort selectable inline, defaulting to the suggestion | 6 |
| Launch failure surfaces the error **and** copies the prompt | 3, 4, 6 |
| Fake `claude` on `PATH`, exact argv asserted, never a real session | 2, 3 |
| Indexer links the launch to its transcript by pre-assigned UUID (terminal) or `short_id` prefix (background) | 7 |
| WCAG 2.2 AA on the new controls, both themes | 6 |

Deliberately deferred to Phase 4, with reasons: the `agents` probe (which is what would show a
launched background session as *running* rather than merely *started*), SSE, sparklines, and the
diagnostics view. Also out of scope and stated so it is not "fixed" by accident: the spec's
`~/.bridge/config.toml` — the model and effort lists stay in `config.py` this phase.

**2. Placeholder scan.** No TBDs. Every task states its interfaces concretely enough to implement
without re-deriving a decision, and no step mandates writing a known defect for a later step to
repair.

**3. Type consistency.** Checked across tasks: `mode` uses exactly `terminal` / `background` in
`models.Launch`, the `launches` DDL, `build_bg_argv`, the `POST /api/launch` body, and the CLI's
`--mode`. `outcome` uses exactly `pending` / `started` / `failed` in the DDL,
`set_launch_outcome`, and `LaunchResult`. Handoff `status` continues to use Phase 2's
`queued` / `consumed` / `dismissed` / `superseded`, and Phase 3 adds no new value — consumption
reuses `consumed`. `LaunchSpec.session_id` is meaningful in terminal mode only: it is the string
written to `launches.session_id` and matched against `sessions.id` in Task 7, and nothing regenerates
it. In background mode it is unset, `short_id` carries the identity, and Task 7 resolves it.

**4. Spec divergence, stated deliberately.** The spec says of the launcher: "Generates the session
UUID itself and writes a `launches` row **before** spawning, so a session is correlatable even if the
spawn fails," and "Because the UUID is pre-assigned, the indexer links the launch to its transcript on
the next scan." That holds for terminal mode and is **not implementable for background mode**:
`claude --bg` discards `--session-id` and mints its own. The plan keeps the *intent* — every launch
gets a row before the spawn, and every launch is correlatable — and changes the *mechanism* for one
mode. The spec is wrong on a factual point about the tool, not on the requirement, so the plan
diverges rather than the spec being amended. Flagging it here so a later reader does not "restore"
`--session-id` to the background argv on the spec's authority; Task 2 has a test whose only job is to
prevent exactly that.

One inconsistency found and resolved during review: the plan originally asserted terminal mode as
"byte-exact modulo trailing newlines" while background mode was byte-exact with no qualifier, which
would have left two different correctness standards in the suite. Resolved by normalising with
`rstrip("\n")` at prompt-file write time, so both modes are held to the identical assertion. The
asymmetry is removed rather than documented.

A second one, noted rather than resolved: `Store.set_launch_outcome` takes a bare string, so nothing
in the type system prevents an invalid outcome. That matches how `set_handoff_status` already works
in Phase 2, and diverging here would be worse than the gap. Left alone deliberately.

---

## Execution Handoff

Plan complete and saved to
`docs/superpowers/plans/2026-07-31-bridge-phase3-launcher.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast
iteration. Tasks 1 and 2 are independent of each other and can run in parallel; Task 3 consumes both;
Tasks 4–6 depend on Task 3; Task 7 depends on Task 3 only.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with
checkpoints.

Confirm Decisions 1 and 2 before Task 3 begins. Everything before that point is reversible without
rework.
