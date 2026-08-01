# Bridge Phase 4 — Amendments from competitive research

**Amends:** `2026-07-31-bridge-phase4-live.md`. That plan is otherwise intact; this document
changes named tasks and adds two. Where they disagree, this document wins, and says why.

**Provenance:** a deep-research survey of 18 comparable tools (`~/Downloads/deep-research-report.md`),
then four source-reading passes over the five closest projects, then local measurement against
`claude` 2.1.220 on this machine. **Every claim below that affects code was measured here, not read.**
The survey itself was wrong about three load-bearing facts, so it is cited for direction only.

---

## Measured corrections to the plan

Phase 4's plan already carries a "spec corrections measured this session" table, because the design
spec described `agents --json` wrongly. The same exercise, run against the plan itself, found four more.

| Plan says | Measured reality | Consequence |
|---|---|---|
| `agents --json` entries have `status` ∈ `idle`\|`busy` | **Two record shapes share one array.** `kind: "interactive"` → `pid` + `status`; `kind: "background"` → `id` + **`state`**, with *no* `pid` and *no* `status` | Task 3's `entry.get("status") or "idle"` labels a **running background agent `idle`** — the exact false-quiescence the plan's own constraints forbid. Fixed in Task 3 below |
| The probe is `claude agents --json` | It is a view over `~/.claude/sessions/<pid>.json`, which carries **more**: `statusUpdatedAt`, `updatedAt`, `version`, `procStart`, `entrypoint` | Task 3 reads the registry as primary. See the cost table |
| (not considered) | Subprocess **250–410 ms** (3 runs) vs registry read **0.1 ms** — ~3000× | Per SSE tick, per connected tab. The plan's 2.0 s timeout also has thin margin over a 0.41 s worst case |
| (not considered) | `procStart` is **UTC**; `ps -o lstart=` is **local time**. Same instant, 5 h apart as strings | A PID-reuse guard that string-compares them always fails. Same class as the ms-vs-seconds trap the plan already documents |

The survey's own three errors, for the record: it claimed `agents --json` gained a `waitingFor` field
(**it has not** — no such field exists); it claimed Conductor OSS stores sessions and attempts in
SQLite with a "sensible separation between durable records and runtime transport" (**its `SessionRepo`
has zero callers outside its own tests; there is no attempts table; sessions are one JSON file per
session written with a bare `write`, not temp-and-rename**); and it recommended `Last-Event-ID` replay
that its own exemplar does not implement anywhere. Treat the survey as a map of what exists, not as a
description of how anything works.

---

## Task 0 — Count one response's tokens once **[DONE, branch `fix-token-dedup`]**

Not in the original plan, and it must land before Task 6 renders anything.

`transcripts.py` summed `usage` per assistant entry. Claude writes one API response as several
entries (thinking, text, tool_use), each repeating that response's usage verbatim. Measured across
60 real transcripts: **1,052 multi-entry requestIds, all contiguous, all byte-identical, naive
summation 199% high.** Every token number on every card was ~3× reality.

Contiguity (0 splits in 1,052) is what makes remembering only the last counted `requestId`
sufficient, and keeps the state one string — which is what lets it persist across an incremental
scan boundary. That boundary case matters more than it looks: it only affects transcripts still
being written, so without the new `sessions.last_usage_request_id` column the inflation would have
survived *precisely for running sessions*, the ones Phase 4 puts on a card.

Shipped: 7 tests, 6 mutations, all caught. `assistant_msgs` deliberately still counts entries.

> **Operational step, not yet run.** The fix corrects new scans only; existing rows keep their
> inflated totals until the files are re-read. After merge:
> `sqlite3 ~/.bridge/bridge.db 'DELETE FROM scan_state;' && bridge index`
> `upsert_session` overwrites totals with `excluded.*`, so this recomputes cleanly. It re-reads
> 3.5 GB once. **This rewrites real data — run it deliberately.**

## Task 1 — The model catalog

**Unchanged.** The catalog shape, the label/value split, and `model_options` all stand.

## Task 2 — Permission mode, not a bypass boolean

**Widened.** `claude --permission-mode <mode>` exists on the main command, and
`--dangerously-skip-permissions` is documented by the CLI as *"Alias for `--permission-mode
bypassPermissions`"*. So the plan's boolean is a narrower version of a control that costs the same
to wire: offer the enum (`default` / `plan` / `acceptEdits` / `bypassPermissions`).

Everything the plan says about the *danger* still holds and now applies to one enum value: never
sticky, never pre-armed from a handoff, conspicuous rather than neutral, and emitted as a fixed
literal. Keep the plan's mutation "default to bypass → every launch is silently bypassed" as the
highest-value test in the phase.

**Add if launch profiles ever accept free-form flags:** Conductor keeps a case-insensitive denylist
(`--dangerously-skip-permissions`, `--yolo`, `--no-permissions`, `--skip-permissions`, `--trust`,
`--full-auto`) on caller-supplied args. Cheap, and the only thing standing between a text field and
an unintended bypass.

## Task 3 — The liveness sensor

**Substantially revised.** Three changes.

**(a) Handle both record shapes.** Never default a missing `status` to `idle`. Interactive entries
carry `status`; background entries carry `state` (`working` / `blocked` / `done` / `failed`, terminal
set `{done, failed, stopped}` — extracted from the 2.1.220 binary). A record with neither is
`unknown`, never `idle`.

**(b) Read `~/.claude/sessions/*.json` as the primary sensor**, keeping `claude agents --json` as
corroboration on a slow cadence. 0.1 ms vs 250–410 ms, and it yields `statusUpdatedAt` — the
staleness input Task 5's hysteresis needs — plus `version`, which is the schema-drift logging the
survey rightly recommends. This is consistent with Bridge's existing bet: it already depends
wholesale on `~/.claude/projects/**/*.jsonl` internals.

Two guards the subprocess was giving away free:
- **Stale pid files.** `os.kill(pid, 0)` checks existence and sends no signal, so it does not breach
  "Bridge never supervises". Cross-check `procStart` against the live process start time to defeat
  PID reuse — **parse both as instants; `procStart` is UTC and `ps` is local.** Conductor's
  equivalent probe is unguarded against reuse; don't copy that.
- **Background agents live elsewhere** (`~/.claude/daemon/roster.json`, `daemon/dispatch/`).

**(c) Retain unrecognized states verbatim.** Conductor's one genuinely good idea is an `Other(String)`
catch-all in its state enum, round-tripping unknown values. Bridge's `status` is already a plain
`str`; keep it that way and never validate against a closed set. Store the raw payload alongside the
normalized projection.

## Task 4 — Last good git state

**Unchanged.** The write-only-on-`ok`, read-only-on-`unavailable`, `not_a_repo`-falls-through design
is right, and the "first timeout overwrites the state it should fall back to" mutation is the subtle
one worth keeping.

## Task 5 — The live band, attribution, and rank

**Two additions.**

**(a) Fix attribution before shipping the band.** The plan's `by_project` maps `cwd` through the
alias table and falls back to itself, which **silently drops any session whose cwd is not a
registered project**. Measured against the 30 registered projects and the live sessions at the time:
`/Users/mitsheth/dev/projectY/boardwatch` matched exactly, but `/Users/mitsheth` matched nothing and
vanished. A dashboard whose headline feature is "what's running right now" must not lose rows.

Required order: **exact match → longest registered-path prefix → an explicit "unattributed live
sessions" bucket.** Exact-before-prefix matters: `boardwatch` is registered *and* sits under
registered `projectY`, and the more specific one is right.

**(b) Hysteresis and a stable ladder.** Do not flip `busy → idle` on one quiet sample. Two
independent codebases converged on ~1.5 s (`ccmanager`'s `IDLE_DEBOUNCE_MS = 1500` after a
500→1000→1500 ladder across four issues; Conductor's `RUNTIME_STATUS_CACHE_TTL = 1500ms`). Use that
shape, **not** muxara's 300 s cool-off — that exists to paper over terminal scraping, which Bridge
does not do. `statusUpdatedAt` makes the check nearly free.

Sort by muxara's priority ladder — `NeedsInput=0, Errored=1, Working=2, Idle=3, Unknown=3` — keeping
its deliberate rank collision so equal-priority rows hold a stable secondary sort instead of
reshuffling on every poll. Debounce **server-side, before emitting**, and key deltas on
`(session_id, state)` so a flapping classification never becomes a client-visible event.

## Task 6 — Sparklines

**Unchanged in design, now correct in arithmetic.** The plan's insistence that `token_series` match
`token_totals` exactly was the right instinct; both were simply wrong in the same direction, which is
what hid it. Keep the test that pins them together — after Task 0 it pins them to a true number.

## Task 7 — Diagnostics

**One addition:** record the `claude` version observed by the sensor (the registry files carry it)
and which sensor answered. When the schema next drifts, that is the difference between a diagnosis
and a bisect.

## Task 8 — SSE

**Simplified and corrected.** Conductor's entire realtime core is a 34-line `routes/events.rs`, and
its pattern is the one to copy: **full snapshot on connect → deltas after → a named `refresh` event
when the channel lags → client resyncs over REST.**

- **Drop `Last-Event-ID`.** The survey specified a monotonic event id, a retained window, and replay.
  Conductor — the exemplar it drew that from — has **zero occurrences** of `Last-Event-ID` and never
  sets an SSE event id. It does not need to: every reconnect begins with a complete snapshot. This
  *deletes* the plan's "requested event older than the window" branch.
- **Add tombstones.** The planned payload (`{"live": {"<path>": {...}}}`) can say "busy" but cannot
  say **"this session is gone"** — a card would keep a live band for a dead session until reload.
  Conductor carries `removedSessionIds` plus a `changedProjectIds` dirty set. Cheap now, awkward later.
- **Cap the stream** (~5 min) and let `EventSource` reconnect, rather than an unbounded generator.
- **Emit only on change.**
- **Reconnect health gate:** reset the backoff counter only once a connection has *proved* healthy
  (Conductor: `validFeedFrameCount >= 2 || (>= 1 && connectedForMs >= 1000)`). Naive reset-on-connect
  turns an accept-then-close server into a hot loop.
- **Name the events.** Conductor has only two named events and discriminates the rest inside the JSON,
  leaving its wire format schema-less. Naming yours gives free client-side dispatch.
- **A one-shot post-launch re-check** (~1.5 s) for the "I just launched — did it take?" moment.

The plan's `X-Accel-Buffering: no`, its refusal to re-render the handoff `<textarea>`, and its
lock-released-before-sleep constraint are all already correct and all stay.

**New constraint, from Conductor's own postmortem** (`docs/terminal-architecture-review.md`, which
reads as a confession): *session status must never depend on client connectivity.* They shipped three
separate bugs from conflating terminal identity with UI visibility, with auth-token identity, and
with browser connection state. Gating poll *cadence* on connected SSE clients is fine; deriving
*status* from it is not.

## Task 9 — Hooks for `needs_input` **[GATED ON A DECISION]**

The one genuine capability addition available, and the only route to a state polling cannot see.

**Verified present in 2.1.220:** `PermissionRequest` (80 refs), `StopFailure` (17),
`allowedHttpHookUrls` (4), `SubagentStart`, and `Notification` with `notification_type` ∈
{`permission_prompt`, `agent_needs_input`, `idle_prompt`, `agent_completed`, …}. Also confirmed:
**no JSONL entry type records a permission prompt** — `claude-code-viewer` can only see permission
requests for sessions *it* spawned via the Agent SDK, which is never Bridge's case. The best
transcript-only proxy anyone has managed is *a `tool_use` with no matching `tool_result` + mtime
< 60 s*. Hooks or nothing.

Because `type: "http"` hooks exist, Bridge receives these on a FastAPI route with **no shim script** —
which also deletes the failure mode `claude-code-agent-monitor` had to engineer around (its handler
is fire-and-forget with a hard `process.exit` backstop, because *waiting* on the response made Claude
Code visibly hang at "running hooks").

**Why this is gated:** it writes `~/.claude/settings.json`, which affects **every** Claude session on
this machine, not only Bridge-launched ones. Constraints if adopted: an explicit short `timeout` on
every entry, never a non-zero exit (exit 2 *blocks* the agent), `async: true` where possible, and
**JSONL indexing stays the reconciliation source of truth** because hook events are silently lost
whenever Bridge is down. Guard for `background_tasks` on `Stop` (real but undocumented) or a session
with a running background task reports finished early.

---

## Rejected, with reasons

- **tmux as a required runtime** — eight of the surveyed tools depend on it for persistence; it would
  trade one AppleScript boundary for session naming, sockets, pane scraping and cleanup.
- **Terminal-output scraping, in any form.** The decisive finding of the whole exercise.
  `ccmanager`'s `stateDetector/claude.ts` took **20 commits in 7 months**, two same-day revert pairs
  (#268/#269 eleven minutes apart; #277→#279→#281), with commit titles reading *"improve busy state
  detection for current Claude Code UI"*. Its busy detector depends on Claude's randomized gerunds
  *ending in "ing"* followed by U+2026. And issue #227 broke with **no version change at all** —
  enabling a `statusLine` removes `esc to interrupt`. Bridge not owning the terminal makes this entire
  failure class unreachable.
- **AI-verified auto-approval.** Beyond "another model thought it was safe": ccmanager's ~45
  dangerous-command regexes use **curly quotes** (`['”]?`), so straight-quoted `rm -rf "/foo"` walks
  past the blocklist; and on approval it writes `'\r'`, selecting whatever option is *highlighted*, so
  drift in Claude's default selection silently changes what gets approved.
- **Copying Claude's session directory** as the handoff mechanism. ccmanager encodes paths as
  `replace(/[/\\.]/g, '-')` but Claude also maps **spaces** to `-`; `/Users/mitsheth/Documents/Vandit
  - Zeel` → `-Users-mitsheth-Documents-Vandit---Zeel`, so its copy silently no-ops. Bridge already
  refuses to decode directory names (`registry.py:3-6`) and takes real paths from `cwd` inside
  transcripts — keep that.
- **`Last-Event-ID` replay** — Task 8.
- **Worktree-per-launch as the data model** — a handoff may describe the main checkout, a debugging
  session that should not branch, or work outside git.
- **An IDE surface** — diff viewers, previews, Kanban, PR automation. Vibe Kanban grew into one and is
  sunsetting; Conductor forked it and now carries a 6,677-line dispatcher and eight polling timers
  layered over a working SSE stream.

## Still true, and worth keeping

No surveyed tool treats an authored next-session brief as a durable first-class artifact; the
alternatives are all transcript continuity (fork, resume, copy the session dir). And none uses the
guarded prompt-file → stdin path — Conductor passes the prompt as the final positional argv and
inserts `--` only when `--mcp-config` is present, so a prompt beginning with `-` can be misparsed.
Both differentiators survived contact with the actual code.
