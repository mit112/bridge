# Bridge Phase 4 — Amendments from competitive research

> **STATUS 2026-08-01: Phase 4 is complete.** All nine tasks shipped on branch
> `phase4-live`; 485 tests, 108 Phase 4 mutations, all caught. Task 0 merged to
> `main` and the data rebuild has been run (measured 2.733× inflation removed
> across 7,706 sessions, zero sessions increased).
>
> **The mutation-anchor debt below is closed.** 512 tests; a full sweep of all
> 24 specs reports 230 caught, 0 survived, 1 deliberate must-survive. Repairing
> the anchors uncovered one genuine SURVIVED they had been masking — see "Known
> debt" at the foot of this file for why a drifted anchor hides the mutations
> behind it.
>
> **Building it corrected this document three times.** See
> "Measured while building" at the foot of this file before trusting the
> `--permission-mode` list or the sensor cost figure above.

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

> **Operational step — RUN 2026-08-01.** Merged to `main` at `474baf5`, then
> `DELETE FROM scan_state` + `bridge index` (7,616 files, 0 parse errors, 10 s).
> Measured across the 7,706 sessions present before and after: **111,889,062 →
> 40,940,856 tokens, an inflation factor of 2.733×, and zero sessions went up.**
> That lines up with the 2.99× predicted from the 60-transcript sample.
>
> The original note, for the record: The fix corrects new scans only; existing rows keep their
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

*Amended 2026-08-01: the corroborator is gone.* `agents.probe_subprocess` shipped and then acquired
zero callers — nothing ever ran it on a slow cadence or on any cadence, so it corroborated nothing
and was deleted along with its tests. What it left behind is a second parser of the same records,
free to drift from the registry path that actually answers. If corroboration is wanted back, it
needs a caller and a cadence decided first, and it should reuse `_session_from` rather than
re-derive the shape.

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

## Task 9 — Hooks for `needs_input` **[INSTALLED 2026-08-01]**

> Shipped exactly as decided. `POST /api/hooks` built and tested first (28
> tests, 12 mutations), then `~/.claude/settings.json` written additively —
> the three new events, each `type: "http"` with `timeout: 2`, plus
> `allowedHttpHookUrls` pinned to `http://127.0.0.1:8787/api/hooks`. The four
> pre-existing hook events were verified byte-identical afterwards, and a
> backup sits at `~/.claude/settings.json.bridge-backup`.
>
> Verified end to end against a real session: posting a `permission_prompt`
> put `needs_input` on the bridge card and on the SSE wire; `agent_completed`
> cleared it. The running `bridge serve` was restarted, because the instance
> that was up predated the route and answered the new hook URL with a 404.

**Mit's decision, taken with the measured risk below in hand. Do not relitigate it.** Install into
`~/.claude/settings.json`: **`Notification`, `SessionStart`, `SessionEnd` only** — all non-blocking
events, all `type: "http"` posting to Bridge's localhost URL, each with an **explicit `timeout: 2`**,
and `allowedHttpHookUrls` pinned to that URL.

Three constraints that made this the safe shape:

- **`timeout` is not optional.** The HTTP hook path is `await Bv.post(...)` — Claude Code waits on
  the response — and the default is `xm = 600000` ms, i.e. **10 minutes**. Bridge *not running* costs
  nothing (`ECONNREFUSED` returns immediately); Bridge running-but-wedged with a defaulted timeout
  stalls a hook for ten minutes. One field is the whole mitigation.
- **`Notification`, not `PermissionRequest`.** Its `notification_type` already distinguishes
  `permission_prompt` / `agent_needs_input` / `idle_prompt` / `agent_completed`, so it carries the
  same signal, and unlike `PermissionRequest` it cannot block a turn.
- **Additive only.** Mit's existing global hooks are on `UserPromptSubmit`, `PreToolUse`,
  `PostToolUse` and `Stop` (swift-format, attribution guard, the max-parallelisation trigger). None
  of the three events above collides, so no working hook is edited. Keep it that way — the fuller
  telemetry variant was rejected precisely because it appends to two of those.

**Order of work: build and test the receiving route BEFORE touching `settings.json`.** Hooks pointed
at a route that does not exist would make every session on the machine POST into a 404. The settings
edit is the last step of this task, not the first.

Rejected in the same decision: a project-scoped trial in one repo (would not give cross-project
needs-input, which is the point), and skipping hooks entirely.

---

### Background: why hooks at all

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
  `replace(/[/\\.]/g, '-')` but Claude also maps **spaces** to `-`; `~/Documents/Client
  - Archive` → `-Users-you-Documents-Client---Archive`, so its copy silently no-ops. Bridge already
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

---

## Measured while building (2026-08-01)

This document corrected the plan; implementing it corrected this document. Each
row below was measured against the installed 2.1.220 binary, and the first
would have failed every launch that used the control it describes.

| This document says | Measured reality | Consequence |
|---|---|---|
| Offer the enum `default` / `plan` / `acceptEdits` / `bypassPermissions` | 2.1.220 accepts exactly **`acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`**, and rejects `default` with *"Allowed choices are…"* | `default` belongs to **settings.json's `permissions.defaultMode`** (`"default" \| "plan" \| "acceptEdits" \| "dontAsk"`, extracted from the binary) — a different, smaller set. The two are easy to conflate and the flag fails outright. Task 2 offers the six real modes plus a no-flag entry that emits nothing |
| The CLI documents `--dangerously-skip-permissions` as *"Alias for `--permission-mode bypassPermissions`"* | Its help text reads only *"Bypass all permission checks."* | Cosmetic. The alias behaviour is right; the citation was not |
| Registry read is 0.1 ms, ~3000× cheaper than the subprocess | 0.1 ms is the *file read*. The PID-reuse guard needs `ps`, a subprocess — **5.5 ms** for a single session, and once **per session** | Fixed by batching every pid into one `ps -o pid=,lstart=`. Measured **2.3 ms** against **790 ms**: ~340×, not ~3000×, but flat in the number of sessions. The unbatched version would have handed most of the advantage back on every SSE tick |

`procStart` UTC vs `ps` local reproduced exactly as documented: the same process
reads `Sat Aug  1 07:44:58 2026` in the registry and `Sat Aug  1 02:44:58 2026`
from `ps`.

### Four tests that passed against their own mutations

Each was a test asserting nothing, found only because the mutation survived:

1. **The SSE store-lock test** — TestClient consumes a streaming body by
   *pulling*, so between frames the generator is parked on its yield and has
   never reached the sleep. A concurrent request cannot overlap it. Replaced by
   a probe inside a stubbed `sleep` that tries the lock **from another thread**:
   `Store._lock` is an RLock, so a same-thread check proves nothing.
2. **`model_options` aliasing** — passed an off-catalog suggestion, which takes
   the prepend branch and builds a fresh list anyway, never reaching the
   `return list(catalog)` it meant to pin.
3. **The hysteresis flap test** — the gap was shorter than the hold, so the
   stale timer had not expired either way.
4. **The nonzero-exit test** — empty stdout fails JSON parsing regardless, so it
   passed with no returncode check at all.

One mutation was **equivalent code**: `0 <= bucket` in `token_series` was
unreachable given the SQL window. The redundant guard was removed rather than
the mutation weakened.

### A falsification trap the plan did not have

A mutation that removes a **loop bound** can make a test *hang* rather than
fail, which takes the falsifier down with it — dropping the SSE time cap left
an `interval=0` stream running forever. Any test exercising a termination
condition needs an independent backstop (here, `max_ticks`) so the mutation
fails fast instead of hanging.

### Known debt, deliberately not fixed here — **closed 2026-08-01**

Seven mutation anchors were **mis-anchored on `main`** and therefore silently
tested nothing: one in `task1-store-and-spool`, four in `task2-api`, one in
`task3-cli`, one in `task5-card-ui`. They pre-dated this branch. The two that
*this* branch invalidated (`phase3-task6`, `task5-card-ui`'s sort-key mutation)
were re-anchored in the same commit as the change that broke them.

**Resolved.** All seven are re-anchored, and a full sweep of every spec now
reports **230 caught, 0 survived**, plus `harness-selftest`'s one deliberate
must-survive. Three things are worth carrying forward:

- **Four matched zero times, two matched twice.** Both are hard errors, so the
  count above was never "seven missing tests" — the ambiguous pair would have
  mutated two sites at once had the harness allowed it. `bridge launch` having
  grown a second empty-prompt check is what made the `bridge handoff` anchor
  ambiguous.
- **Repairing an anchor exposed a real SURVIVED behind it.** falsify aborts a
  spec at its first bad anchor, so a drifted anchor hides every mutation after
  it. `task5-card-ui`'s "remove the live region from the copy confirmation"
  had never once been evaluated; it survived, because the card renders three
  status lines and the test asserted a bare `role="status"` substring over the
  whole page. A mis-anchored spec is not merely untested, it is *masking*.
- **The blind spot is structural, so it now has a standing check.**
  `tests/test_mutation_specs.py` asserts every anchor still matches its
  `expect_count` and every named test still exists, reporting all offenders in
  one run rather than one per commit-and-rerun cycle. Verified by perturbation,
  not by passing: drift an anchor and rename a test, and it fails on both.
