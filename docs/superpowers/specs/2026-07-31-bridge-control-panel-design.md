# Bridge — Claude Code Project Control Panel

**Date:** 2026-07-31
**Status:** Approved design
**Working name:** Bridge (renameable; affects repo dir, CLI name, and default port only)

## Problem

Claude Code sessions across ~20 active projects leave their state scattered and unreadable:

- **Session history is recorded but has no reader.** `~/.claude/projects/` holds 9,229 JSONL
  transcripts (3.5 GB) across 45 directories. Each carries the session title, prompts, model,
  effort, git branch, and per-message token usage. Nothing surfaces any of it.
- **Handoff prompts are juggled by hand.** Sessions habitually end by asking Claude for a prompt
  to open the next session. Those prompts get pasted into whatever file is nearby. The result on
  disk: `projectY/NEXT-SESSION-HANDOFF.md`, `portfolio-website/v2/HANDOFF.md`,
  `portfolio-website/v2/NEXT-SESSION.md`, `claudeTest/design/HANDOFF.md` — four names, four
  locations, no index, no link back to the session that produced them.
- **Starting the next session is manual.** Find the prompt, open a terminal, `cd`, pick a model
  and effort, paste.
- **Risk is invisible.** Uncommitted work ages silently. One project accrued 47 modified files
  across 33.8 hours with zero commits, and nothing surfaced that.

## Goal

A locally hosted control panel that shows the true state of every project at a glance, stores the
next-session prompt for each one, and launches a Claude Code session from that prompt with a
chosen model and effort. It becomes the place all Claude Code sessions are driven from.

Concurrent sessions across multiple projects read from and write to it, so it must stay correct
under concurrent access and must never lose a handoff.

## Non-goals

- Not a Claude Code replacement or a chat UI. It launches and observes sessions; it does not host
  them.
- Not multi-user, not internet-facing, no cloud sync.
- Not a git client. It reports git state; it never mutates a repo.
- Not a cost governor. It reports token burn; it does not throttle.

---

## Architecture

One local server process. It is the **sole writer** to a SQLite database in WAL mode. Everything
else — the web UI, the CLI, Claude sessions — is a client.

```
  Claude session ─┐
  Claude session ─┼─► bridge CLI ──HTTP──┐
  /handoff cmd   ─┘                      │
                                         ▼
   ~/.claude/projects/*.jsonl ──►  ┌────────────┐
   git repos (read-only)     ──►   │   server   │──► SQLite (WAL)
   claude agents --json      ──►   │            │
   session-meta/*.json (opt) ──►   └─────┬──────┘
                                         │ HTML + SSE
                                         ▼
                                    web UI (browser)
                                         │ launch
                                         ▼
                              osascript → Terminal   |   claude --bg
```

**Why sole-writer:** many concurrent sessions must be able to write. Funnelling every write
through one process gives zero lock contention and no torn state, and keeps the CLI stateless.

**Durability requirement:** if the server is unreachable when a session writes a handoff, the CLI
writes it to `~/.bridge/spool/<uuid>.json` and the server drains the spool on next boot. A handoff
is never lost.

**Read-only on user code.** Bridge's only writes are its own database and spool. It never writes to
a project repo.

### Stack

FastAPI + Jinja2 + HTMX + SSE, plain CSS. No build step, no `node_modules`, one language.

Python 3.13, pinned via `uv` (the system Python is 3.9.6 and is not used). Dependencies: `fastapi`,
`uvicorn`, `jinja2`, `httpx` (CLI only), `pytest` + `pytest-asyncio` (dev). `sqlite3` from stdlib.

Rejected: a separate Vite/React frontend (two toolchains and a build step for a tool that should
just be running); Node/TS full-stack (adds a bundler, and the transcript indexer is plainer in
Python). The API boundary is clean, so replacing the frontend later is a contained change.

Bound to `127.0.0.1:8787`. No authentication. LAN access is explicitly out of scope for this
version — it requires a token, and an unauthenticated session-launcher must not reach the LAN by
accident.

---

## Data sources

| Source | Use | Trust |
|---|---|---|
| `~/.claude/projects/**/*.jsonl` | Sessions, titles, prompts, model, effort, branch, **token usage** | Authoritative |
| Project git repos | Branch, dirty count, ahead/behind, last commit, uncommitted age | Authoritative, read-only |
| `claude agents --json` | Currently running sessions (interactive + background) | Authoritative, may fail |
| `~/.claude/usage-data/session-meta/*.json` | Enrichment: `tool_counts`, `languages`, `git_commits`, `duration_minutes` | **Optional, incomplete** |

`~/.claude/cost-tracker.log` is **not** a source. Despite its name it logs tool invocations
(`tool=Bash command=…`) and contains no token or cost data.

`session-meta/` is `/insights` output, capped at 200 sessions newest-first. Read opportunistically
when a file for a session ID exists; never required, never blocking, never the basis of a total.

### Transcript record shapes relied upon

Per JSONL line, keyed by `type`:

- `ai-title` → `aiTitle`: the session's generated title. Latest occurrence wins.
- `last-prompt` → `lastPrompt`: most recent user prompt text.
- `user` / `assistant` → `message`, `timestamp`, `cwd`, `gitBranch`, `sessionId`, `isSidechain`.
- `assistant` → `message.model`, `effort`, and `message.usage` with `input_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`.

Token totals sum `usage` across `assistant` records where `isSidechain` is false, plus a separate
sidechain subtotal so subagent burn is attributable but not double-counted.

Unknown `type` values are ignored, not errors. The reader tolerates absent keys throughout: the CLI
version that wrote a transcript varies across a 3.5 GB corpus spanning months.

---

## Components

Eight units. Each has one job, a typed interface, and independent tests.

### 1. `transcripts`
JSONL → typed `SessionRecord`. Pure functions over paths; stdlib only.

`SessionRecord`: `session_id`, `project_path` (from `cwd`), `title`, `started_at`, `ended_at`,
`model`, `effort`, `git_branch`, `user_message_count`, `assistant_message_count`, `last_prompt`,
`tokens` (in/out/cache-create/cache-read), `sidechain_tokens`, `parse_errors`.

**This is the one real engineering problem.** A 3.5 GB corpus cannot be re-parsed per refresh.

- Persist `(path, size, mtime, parsed_offset, session_id)` per file in `scan_state`.
- Re-parse only the byte range past `parsed_offset`. If `size` shrank, treat the file as rewritten
  and re-scan from zero.
- For a card, the first line plus the trailing ~200 lines yield everything needed. Full parse
  happens only on demand for the detail view.
- Streaming line-at-a-time. A whole file is never held in memory.
- **Target: incremental refresh under 200 ms** on the existing corpus, with a test asserting the
  work done stays proportional to the delta rather than the corpus.

### 2. `gitprobe`
Repo path → `branch`, `dirty_count`, `ahead`, `behind`, `last_commit_summary`, `last_commit_at`,
`oldest_uncommitted_at`. Shells out to `git` via absolute path with a 2-second timeout. Never
mutates. `oldest_uncommitted_at` is the oldest mtime among tracked-and-modified files, which is
what makes the staleness warning meaningful.

**Never strip `git status --porcelain` output as a whole.** Porcelain encodes status in the first
two columns, and an unstaged modification is ` M path` with a leading space. Stripping the full
stdout shifts that line left, so a fixed `line[3:]` slice mangles the path, `stat()` raises
`OSError`, and the file is silently dropped from the age computation while still counted in
`dirty_count` — corrupting the staleness signal for the most common git state there is. Strip only
where a scalar is wanted (branch, counts, log); split porcelain raw. Rename entries (`R old -> new`)
must have the arrow split off *before* quotes are stripped, or a quoted destination keeps a stray
leading quote.

### 3. `agents`
Wraps `claude agents --json` → list of live sessions with `session_id`, `cwd`, `model`, `effort`,
`started_at`. Tolerates non-zero exit, malformed JSON, and unexpected shape by returning
`unavailable`.

### 4. `store`
SQLite schema and queries. Sole writer. WAL mode, `foreign_keys=ON`, `busy_timeout=5000`.

```
projects(id, path, name, status, pinned, created_at)
    status ∈ {active, archived, hidden}
sessions(id, project_id, title, started_at, ended_at, model, effort, git_branch,
         user_msgs, assistant_msgs, tokens_in, tokens_out, tokens_cache_create,
         tokens_cache_read, sidechain_tokens, transcript_path)
handoffs(id, project_id, source_session_id, summary, next_prompt, suggested_model,
         suggested_effort, status, created_at, consumed_at)
    status ∈ {queued, consumed, dismissed}
launches(id, project_id, handoff_id, session_id, mode, model, effort, prompt,
         launched_at, outcome)
    mode ∈ {terminal, background}; outcome ∈ {pending, started, failed}
scan_state(transcript_path PK, size, mtime, parsed_offset, session_id)
git_cache(project_id PK, payload_json, probed_at)
```

Migrations are **additive only** — new columns and tables, never a table rebuild.

SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so replaying a schema
list on every open cannot add a column: the second open raises `duplicate column
name`. Column evolution therefore goes through a `COLUMN_MIGRATIONS` map plus an
`_ensure_columns()` step that consults `PRAGMA table_info` and issues `ALTER
TABLE` only for columns genuinely absent. Tables keep using `CREATE TABLE IF NOT
EXISTS`. This is what makes the additive-only doctrine executable rather than
aspirational.

### 4b. Path aliasing (Phase 2)

Projects move. Seven of this machine's transcript `cwd` values point at old
`~/Documents/...` locations for projects that now live under `~/dev`, so the same
logical project appears twice with split history. Approved resolution: **alias old
paths to canonical ones and merge the history**, rather than archiving the old
halves.

```
project_aliases(alias_path TEXT PRIMARY KEY, canonical_path TEXT NOT NULL)
```

`indexer._index_one` maps `rec.project_path` through `project_aliases` before
calling `upsert_project`, so sessions recorded under an old path attribute to the
canonical project. Verified mappings (all targets confirmed present on disk):

| alias_path | canonical_path |
|---|---|
| `~/Documents/Job apps` | `~/dev/Job apps` |
| `~/Documents/projectX` | `~/dev/projectX` |
| `~/Documents/projectX/hookrail` | `~/dev/projectX/hookrail` |
| `~/Documents/claude-stuff/dota2` | `~/dev/claude-stuff/dota2` |
| `~/Documents/claude-stuff/Houston social` | `~/dev/claude-stuff/Houston social` |
| `~/Documents/anhkhooey` | `~/dev/anghkooey` |
| `~/dev/StreakSync/.worktrees/streaksync-ui-polish` | `~/dev/StreakSync` |

Two notes on that table. The `anhkhooey` → `anghkooey` entry is a rename as well as
a move — the spellings genuinely differ. The StreakSync entry folds a deleted
worktree into its parent repo, which is a judgment call: the work landed in the
parent, so its sessions belong there.

`~/Documents/Client Archive/OLDPROJECT` has **no alias target** — the directory is
gone entirely. It gets archived via `set_project_status`, not aliased.

**Re-attribution needs no migration.** The SQLite database is a pure derived cache
of the transcripts; a full rebuild costs ~11 seconds. Any change to attribution,
schema, or parsing is applied by deleting `~/.bridge/bridge.db` and re-indexing.
This is why `COLUMN_MIGRATIONS` matters only for preserving a *running* server's
data, never for correctness.

Wiring this up also requires exposing `set_project_status`, which Phase 1 left
unreachable.

### 5. `registry`
Discovers and classifies projects. Opt-out, not opt-in: auto-discover from transcript directory
names and `~/dev/*` git repos, then auto-hide known noise — `-private-tmp-*`,
`-Users-mitsheth--claude`, `-Users-mitsheth--local-share-ecc-homunculus*` (11 dirs),
`-Volumes-mit-immich`. Hidden projects are toggleable in the UI. Archive, never delete.

Transcript directory names are path-encoded (`/` → `-`), which is lossy: `-Users-mitsheth-dev-Job-apps`
could decode to `Job apps` or `Job-apps`. Resolve the real path from the `cwd` field inside the
transcript, never by decoding the directory name.

### 6. `api`
FastAPI routes.

```
GET  /                              dashboard
GET  /project/{id}                  detail view
GET  /events                        SSE stream
POST /api/handoff                   create handoff        (CLI + /handoff)
GET  /api/handoff/{project_id}      current queued handoff
PATCH/api/handoff/{id}              edit prompt / dismiss
POST /api/launch                    launch a session
POST /api/refresh                   force reindex
GET  /api/projects                  list + status
PATCH/api/projects/{id}             pin / archive / hide
GET  /api/diagnostics               parse errors, probe failures, spool depth
```

### 7. `launcher`
`(project, prompt, model, effort, mode)` → spawned session.

Generates the session UUID itself and writes a `launches` row **before** spawning, so a session is
correlatable even if the spawn fails.

- **terminal**: `osascript` opens a new Terminal window running
  `cd <path> && claude --session-id <uuid> --model <m> --effort <e> --name "<title>" "<prompt>"`.
- **background**: same argv with `--bg` instead, no terminal.

The prompt is passed as a single argv element, never interpolated into a shell string, and the
AppleScript payload is escaped. Prompts are multi-line untrusted text.

Because the UUID is pre-assigned, the indexer links the launch to its transcript on the next scan,
closing the loop: queued prompt → running session → completed session → next handoff.

### 8. `bridge` CLI
Thin HTTP client for Claude sessions and hooks. This is what makes concurrent multi-session writes
work.

```
bridge handoff --project <path> --summary <text> --prompt-file -   [--model M] [--effort E]
bridge status [--project <path>]
bridge open
```

`--project` defaults to `$PWD`, resolved to its registered project. On connection failure, writes
to `~/.bridge/spool/` and exits zero — a session must never fail because the panel is down.

### `/handoff` slash command
A command in `~/.claude/commands/` that composes the session summary plus the next-session prompt
and invokes `bridge handoff`. This is the sole intended capture path going forward.

No Stop hook. Automatic capture on every session stop was considered and rejected: it fires on
every stop regardless of whether the session reached a meaningful boundary, and produces junk
entries in a store whose value depends on every entry being worth launching.

---

## Data flow: the handoff loop

1. A session ends the way it already does — Claude is asked for a prompt to open the next session.
2. `/handoff` runs. It writes `{project, session_id, summary, next_prompt, suggested_model,
   suggested_effort}` to Bridge.
3. The prompt now lives in one place, attached to its project, linked to the session that produced
   it.
4. The project's card shows it. Model and effort are selectable inline, defaulting to the
   suggestion.
5. ▶ launches it. The prompt is **editable in the panel before launch** — tweaking before starting
   is the normal case.
6. The launched session is followed by its pre-assigned UUID and appears as running, then as the
   project's most recent session.

### Backfill

A one-time pass over the 9,229 existing transcripts seeds every project's session history, and
scrapes the four stray `HANDOFF.md` / `NEXT-SESSION.md` files into real queued handoffs.

Backfill summaries derive from `aiTitle` + files touched + commits made + `lastPrompt`. **No LLM
calls** — the backfill is free and fast. Real prose comes from `/handoff` going forward.

Backfill is idempotent and resumable: keyed on `session_id`, re-runnable after interruption.

---

## The card

Cards are sorted by **actionability**, not alphabetically. The first question a card answers is
*does this want me right now?*

Order: queued handoff → running now → dirty and stale → recently active → idle.

```
┌──────────────────────────────────────────────────────────────┐
│ projectY                          ● running · 34m · opus/high│
│ ~/dev/projectY                                               │
│                                                              │
│ Review overnight P5 run                             12m ago  │
│ Verified the P5 candidate catalog, fixed 3 dedup bugs,       │
│ left the boardwatch agent mid-sweep.                         │
│                                                              │
│ main · 47 dirty · ⚠ no commit in 33.8h                       │
│ ▁▂▅█▆▃▁  184k today · 41k last 5h                            │
│                                                              │
│ ▸ QUEUED  Continue the P5 sweep from catalog entry 214…      │
│                                    [opus ▾] [high ▾]  [ ▶ ]  │
└──────────────────────────────────────────────────────────────┘
```

Five bands, one concern each. Reading order is deliberate: identity → live status → what happened →
what's at risk → what it cost → what's next.

### Presentation rules

- **Color carries meaning only.** One accent for *running*, one warning for *risk*. Nothing else is
  colored; everything else earns attention through weight and whitespace.
- **One number per concern, unit implied.** `47 dirty`, not `Uncommitted changes: 47 files`.
- **The ⚠ is the highest-value element on the card.** Uncommitted-work age is the real risk signal.
  It is the only warning treatment, so it never competes for attention. It appears when
  `oldest_uncommitted_at` exceeds **12 hours**, configurable as `stale_hours` in
  `~/.bridge/config.toml`. A project with no tracked modifications never shows it regardless of age.
- **Single column; two columns at ≥1400px.** Readability was an explicit requirement, and a dense
  grid at this information level stops being scannable.
- Relative timestamps (`12m ago`) with absolute times on hover.
- The sparkline is inline SVG over the last 7 days of that project's token burn.
- **Token burn is reported as absolute counts, never as a percentage of a limit.** The plan is a 5h
  rolling window with no published total cap, so a percentage would have no denominator. The card
  shows tokens today and tokens in the last 5 hours; the top bar shows the same totals across all
  projects plus a current burn rate. These are attribution and rate signals, not budget signals.
- The model and effort selectors are populated from a small config file
  (`~/.bridge/config.toml`, `models = [...]`, `efforts = [...]`), seeded on first run and editable
  by hand. Values are passed through to `--model` / `--effort` unvalidated; the CLI is the authority
  on what is accepted, and a rejected value surfaces as a launch failure.
- WCAG 2.2 AA is non-negotiable: contrast on every text/background pair, status never conveyed by
  color alone (the `●` and `⚠` glyphs carry it too), full keyboard operation, visible focus rings.
- Dark and light both first-class, driven by `prefers-color-scheme`.

`design-guardrails` is applied during implementation of the UI, per the repository's standing rule
for interface work.

### Top bar

Global state: tokens today and in the last 5h across all projects with a current burn rate, count of
running sessions, count of queued handoffs, last index time, and a diagnostics affordance when parse
errors or spool depth are non-zero.

### Detail view

Full session timeline for the project, every past handoff with its outcome, recent git log, and
per-session token breakdown including sidechain subtotals.

---

## Error handling

Every external input is untrusted; every probe may fail without taking down a card.

| Failure | Behavior |
|---|---|
| Malformed JSONL line | Skip, increment `parse_errors`, surface count in diagnostics. Never abort a scan. |
| Truncated final line (session in flight) | Expected, not an error. Leave `parsed_offset` before it. |
| Transcript file shrank | Treat as rewritten; re-scan from offset zero. |
| `git` timeout or non-repo | `git: unknown` on that card only; last good `git_cache` shown with its age. |
| `claude agents --json` fails | Live band renders *unavailable*; rest of card unaffected. |
| Server unreachable from CLI | Spool to `~/.bridge/spool/`, exit zero, drain on boot. |
| Launch fails (osascript denied) | Surface error in UI **and** copy prompt to clipboard, so the user is never stuck. |
| Project path no longer exists | Auto-archive, keep history. |

No failure in any probe may prevent the dashboard from rendering. A card with three failed probes
still renders with what it has.

---

## Testing

pytest. This tool sits on the critical path of all development work, so it gets real tests.

- **`transcripts`** — hand-built fixture JSONL covering: normal session, compacted session,
  sidechain/subagent entries, malformed line mid-file, truncated final line, missing keys, empty
  file, unknown `type` values, file-shrank-since-last-scan.
- **`transcripts` perf** — asserts incremental rescan work is proportional to appended bytes, not
  corpus size.
- **`gitprobe`** — against temp repos created in-test: clean, dirty, detached HEAD, ahead/behind,
  not-a-repo, and a timeout via a stub.
- **`store`** — concurrent-writer test that proves the WAL + sole-writer assumption; additive
  migration test.
- **`registry`** — path-encoding ambiguity (`Job apps` vs `Job-apps`) resolves from `cwd`, not the
  directory name; noise patterns are hidden.
- **`launcher`** — a **fake `claude` on `PATH`**; tests assert the exact argv constructed and that
  prompts with quotes, newlines, and shell metacharacters survive intact. **Never spawns a real
  session.**
- **`api`** — FastAPI `TestClient`; spool-drain-on-boot; handoff lifecycle.
- **CLI** — spools on connection refused and exits zero.

---

## Phasing

One spec, four phases. Each ends somewhere independently useful.

1. **Read-only panel** — `transcripts`, `store`, `registry`, `gitprobe`, card UI. Answers "what is
   the state of all my projects." Zero risk. Includes backfill.
2. **Handoff loop** — `bridge` CLI, `/handoff` command, spool, handoff UI. Removes the copy-paste
   problem.
3. **Launcher** — terminal and `--bg` modes, session-ID correlation, clipboard fallback.
4. **Live** — SSE, `agents` probe, window meter, sparklines, diagnostics view.

Phases share one data model and are not independent enough to warrant separate specs.

## Success criteria

- The dashboard shows every active project's last session, git state, and burn, and renders in
  under 500 ms after a warm index.
- Incremental reindex of the 9,229-file corpus completes in under 200 ms.
- A handoff written by `/handoff` in any project appears on that project's card, and survives the
  server being down at write time.
- A session launched from a card opens in a terminal with the chosen model and effort, and is
  linked back to its own transcript on the next index.
- No probe failure can prevent the dashboard from rendering.
