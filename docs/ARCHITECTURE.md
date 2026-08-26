# Bridge — Architecture

Bridge is a locally hosted control panel for Claude Code work. It reads the state
of every project you drive from Claude Code — session history, git status, token
burn — stores the next-session prompt for each one, and launches a new session
from that prompt. It observes and launches sessions; it does not host them.

This document is for contributors. For usage, see the [README](../README.md).

## Design in one paragraph

One local server process is the **sole writer** to a SQLite database in WAL mode.
Everything else — the web UI, the `bridge` CLI, the Claude sessions that report
into it — is a client. Funnelling every write through one process gives zero lock
contention and no torn state under concurrent multi-session access, and keeps the
CLI stateless. Most of the database — sessions, git state, token counts — is a
pure derived cache of the transcripts on disk and can be deleted and rebuilt at
any time. Handoffs and scheduled runs are the exception: they are **authored**
data with no transcript to rebuild them from, so each is backed by its own
append-only on-disk journal (see Durability below) rather than the transcript
corpus.

```
  Claude session ─┐
  Claude session ─┼─► bridge CLI ──HTTP──┐
  /handoff cmd   ─┘                      │
                                         ▼
   ~/.claude/projects/*.jsonl ──►  ┌────────────┐
   git repos (read-only)     ──►   │   server   │──► SQLite (WAL)
   ~/.claude/sessions/*.json ──►   │  (FastAPI) │
   session-meta/*.json (opt) ──►   └─────┬──────┘
                                         │ HTML + SSE
                                         ▼
                                    web UI (browser)
                                         │ launch
                                         ▼
                              osascript → Terminal  |  claude --bg
```

## Stack

FastAPI + Jinja2 + Server-Sent Events, plain CSS and hand-written JavaScript. No
build step, no `node_modules`, no frontend framework. Python pinned via `uv`.
Runtime deps are `fastapi`, `uvicorn`, and `jinja2`; the CLI talks to the server
over stdlib `urllib`, not a dependency; `sqlite3` is from the stdlib. Server
binds to `127.0.0.1` with no authentication — it is a single-user local tool, and
LAN exposure is deliberately out of scope (an unauthenticated session-launcher
must not be reachable off the loopback).

The API boundary between server and UI is clean, so the frontend is a contained
piece: server-rendered Jinja fragments, swapped into a persistent shell by a
small client-side router (no HTMX, no build step) and kept live by SSE plus an
in-place DOM morph.

## Data sources

| Source | Use | Trust |
|---|---|---|
| `~/.claude/projects/**/*.jsonl` | Sessions, titles, prompts, model, effort, branch, token usage | Authoritative |
| Project git repos | Branch, dirty count, ahead/behind, last commit, uncommitted age | Authoritative, read-only |
| `~/.claude/sessions/*.json` | Currently running sessions | Authoritative, may fail |
| `~/.claude/usage-data/session-meta/*.json` | Enrichment (tool counts, languages, commits, duration) | Optional, incomplete |

Every source is treated as untrusted input. Unknown record shapes and absent keys
are tolerated, never fatal: the corpus spans many months and many CLI versions.

## Modules

Each unit has one job, a typed interface, and independent tests.

- **`transcripts`** — JSONL → typed `SessionRecord`. Pure functions over paths,
  stdlib only. This is the one real engineering problem (see below).
- **`indexer`** — orchestrates incremental scans and upserts into the store,
  mapping moved project paths to a canonical path so split history merges.
- **`gitprobe`** — repo path → branch, dirty count, ahead/behind, last commit, and
  `oldest_uncommitted_at`. Shells out to `git` with a timeout; never mutates.
- **`agents`** — reads the `~/.claude/sessions/*.json` registry directly (no
  subprocess); returns *unavailable* on any read or shape failure.
- **`store`** — the SQLite schema and queries; the sole writer. WAL,
  `foreign_keys=ON`, `busy_timeout`. Migrations are additive only.
- **`registry`** — discovers and classifies projects (opt-out, not opt-in), then
  auto-hides known-noise transcript directories. Archives, never deletes.
- **`api`** — FastAPI routes plus the SSE stream.
- **`launcher`** — `(project, prompt, model, effort, mode)` → a spawned session.
- **`cli` / `__main__`** — the `bridge` CLI: a thin, stateless HTTP client.
- **`spool` / `schedspool`** — on-disk durability queues (see *Durability*).
- **`scheduler` / `schedule_view`** — scheduled sessions.
- **`notify` / `watcher`** — push-based liveness (see *Liveness*).
- **`backfill`** — one-time, LLM-free seed of history from existing transcripts.
- **`cards` / `dashboard` / `overview` / `projects_view` / `sessionmeta` /
  `settings_view`** — the server-rendered surfaces.
- **`config`** — `~/.bridge/config.toml`.

## The incremental indexer

A multi-gigabyte transcript corpus cannot be re-parsed on every refresh, so the
indexer persists `(path, size, mtime, parsed_offset, session_id)` per file and
re-parses only the byte range past `parsed_offset`.

- If a file's size shrank, treat it as rewritten and re-scan from offset zero.
- A truncated final line (a session still in flight) is expected, not an error:
  leave `parsed_offset` before it so the next scan picks it up.
- Parsing is streaming, line at a time; a whole file is never held in memory.
- A card needs only the first line plus the trailing lines; a full parse happens
  only on demand for the detail view.

The invariant that keeps this honest: **work done stays proportional to the delta,
not to the corpus size**, and there is a test that asserts exactly that.

## The gitprobe porcelain gotcha

`git status --porcelain` encodes status in the first two columns, so an unstaged
modification is `` M path`` — a leading space. **Never strip the porcelain output
as a whole**: stripping shifts that line left, a fixed `line[3:]` slice then
mangles the path, `stat()` raises, and the file is silently dropped from the
uncommitted-age computation while still counted in `dirty_count` — corrupting the
staleness signal for the most common git state there is. Strip only where a scalar
is wanted (branch, counts, log); split porcelain raw. Rename entries
(`R old -> new`) must have the arrow split off *before* quotes are stripped.

## The store and additive migrations

SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so replaying a schema
list on every open cannot add a column — the second open raises `duplicate column
name`. Column evolution therefore goes through a migration map plus an
`_ensure_columns()` step that consults `PRAGMA table_info` and issues `ALTER TABLE`
only for genuinely-absent columns; tables use `CREATE TABLE IF NOT EXISTS`. For the
derived tables, migrations only matter for preserving a *running* server's data,
never for correctness — a full rebuild reconstructs everything from transcripts.
`handoffs` and `scheduled_runs` are the exception: their rebuild source is their
own on-disk journal, not the transcript corpus, so losing both the database and
the journal loses them for good.

Core tables: `projects`, `sessions`, `handoffs`, `launches`, `scheduled_runs`,
`scan_state`, `git_cache`, plus a path-alias table that folds moved projects into
one canonical identity. `handoffs` and `scheduled_runs` are the authored tables
called out above — the migration story is the same, but a rebuild after data
loss for them means replaying their own journal, not re-scanning transcripts.

## The handoff loop

1. A session ends by asking Claude for a prompt to open the next session.
2. The `/handoff` slash command writes `{project, session_id, summary,
   next_prompt, suggested_model, suggested_effort}` to Bridge.
3. The prompt now lives in one place, attached to its project and linked to the
   session that produced it. The project card shows it.
4. The card launches it. The prompt is **editable in the panel before launch** —
   tweaking before starting is the normal case.
5. The launcher pre-assigns the session UUID and writes a `launches` row *before*
   spawning, so a session is correlatable even if the spawn fails. On the next
   scan the indexer links the launch to its transcript, closing the loop:
   queued prompt → running session → completed session → next handoff.

There is deliberately **no Stop hook** for automatic capture: it would fire on
every stop regardless of whether the session reached a meaningful boundary, and
fill the store with junk. Capture is an explicit act.

## Durability

A handoff must never be lost. If the server is unreachable when the CLI writes
one, the CLI writes it to an on-disk spool (`~/.bridge/spool/`) and exits zero — a
session must never fail because the panel is down — and the server drains the
spool on next boot. Scheduled sessions have the same guarantee via a second spool,
with claims journaled so a run-now can't double-fire.

## Liveness

Every surface stays live without a manual refresh:

- The server pushes SSE events; the browser morphs the affected fragment in place
  rather than reloading, so scroll position and focus survive.
- Freshness is push-based: a notifier fans out changes to connected clients, and a
  filesystem watcher over the transcript directory turns a new transcript line
  into a sub-second update. An active-surface polling cadence backs it up.
- Navigation uses a persistent shell (no full-page reload between routes); route
  bodies are swapped into a single scroll container.

## Launching a session

- **terminal**: `osascript` opens a new terminal running
  `claude --session-id <uuid> --model <m> --effort <e> --name "<title>" "<prompt>"`
  in the project directory.
- **background**: the same argv with `--bg`, no terminal.

The prompt is passed as a single argv element, never interpolated into a shell
string; prompts are multi-line untrusted text and the AppleScript payload is
escaped. If a launch fails, the UI surfaces the error *and* copies the prompt to
the clipboard, so the user is never stuck.

## Error handling

Every external input is untrusted and every probe may fail without taking down a
card. No probe failure may prevent the dashboard from rendering — a card with
several failed probes still renders with what it has.

| Failure | Behavior |
|---|---|
| Malformed JSONL line | Skip, increment `parse_errors`, surface count in diagnostics. Never abort a scan. |
| Truncated final line | Expected; leave `parsed_offset` before it. |
| Transcript file shrank | Treat as rewritten; re-scan from offset zero. |
| `git` timeout or non-repo | Mark git *unknown* on that card only; show last good cache with its age. |
| Registry read (`~/.claude/sessions/*.json`) fails | Live band renders *unavailable*; rest of card unaffected. |
| Server unreachable from CLI | Spool to disk, exit zero, drain on boot. |
| Launch fails | Surface the error in the UI and copy the prompt to the clipboard. |
| Project path no longer exists | Auto-archive, keep history. |

## Presentation principles

- Cards sort by **actionability**, not alphabetically: queued handoff → running
  now → dirty and stale → recently active → idle. The first question a card
  answers is *does this want me right now?*
- **Color carries meaning only.** One accent for *running*, one warning treatment
  for *risk*; everything else earns attention through weight and whitespace.
- **One number per concern, unit implied** (`47 dirty`, not
  `Uncommitted changes: 47 files`).
- The uncommitted-work-age warning is the highest-value element on the card and
  the only warning treatment. It appears when the oldest uncommitted change
  exceeds `stale_hours` (configurable), and never for a project with no tracked
  modifications.
- Token burn is reported as **absolute counts, never as a percentage of a limit**
  — the usage model is a rolling window with no published total, so a percentage
  would have no denominator.
- WCAG 2.2 AA is non-negotiable: sufficient contrast on every text/background pair,
  status never conveyed by color alone (glyphs carry it too), full keyboard
  operation, visible focus rings. Dark and light are both first-class, driven by
  `prefers-color-scheme`.

## Testing

pytest, and the suite is **hermetic** — it must pass under a clean `$HOME` with no
real transcript corpus present. A test that only passes because of the author's
own `~/.claude` data is a bug, not a pass. Highlights: hand-built fixture JSONL
covering compaction, sidechains, malformed and truncated lines, missing keys, and
file-shrank-since-last-scan; a perf test asserting incremental rescan work is
proportional to appended bytes; temp-repo git probes; a concurrent-writer test for
the WAL sole-writer assumption; a fake `claude` on `PATH` so the launcher tests
assert the exact argv without ever spawning a real session.
