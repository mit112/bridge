# Bridge Freshness Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a long-lived Bridge dashboard honest about data freshness and connection health, while allowing explicit and periodic refreshes to update the already-open page through leaf-only DOM patches.

**Architecture:** Add one server-owned refresh coordinator that serializes explicit and periodic reindex runs, records the last successful generation/error, and shares one JSON update envelope across `POST /api/refresh` and SSE. Keep server-rendered cards and handoff textareas as the DOM identity boundary: the browser patches only named leaves/attributes and appends the existing project `<li>` nodes in the server-provided order.

**Tech Stack:** Python 3.13, FastAPI, SQLite/WAL, Jinja2, plain JavaScript, pytest, Node harnesses already used by `tests/test_static_js.py`, and the existing mutation falsifier. No new dependency, framework, build step, or writable location.

## Global Constraints

- Preserve pinned → queued handoff → running → dirty/stale → recent → idle.
- Preserve one column and two columns only at min-width: 1400px.
- Preserve absolute token counts; no gauges or percentages.
- Preserve read-only git probing, localhost binding, and the sole-writer architecture.
- Keep six-digit lowercase hex color tokens unless the contrast parser and deliberately failing proof change in the same commit.
- Never replace cards or handoff textareas.
- No `innerHTML` for live patching and no page reload.
- Do not implement scheduled-history HTML/pagination, manual theme, or keyboard shortcuts in this lane.
- Do not push.
- Do not add AI attribution or `Co-Authored-By` trailers.

## Verified Baseline and Scope Notes

- The delegated worktree is detached at `734a9342ac8b82401b98e500c6c81b4ac04c385f` (`Keep hidden disclosures collapsed`) and clean. `/Users/mitsheth/dev/bridge` is clean on `main` at the same commit. The detached worktree is intentional App/worktree state; do not create a branch or alter it in this lane.
- The literal requested `.agent/plans/2026-08-02-bridge-ui-ux-cheap-wins.md` does not exist in the commit. The committed counterpart that was read in full is `docs/superpowers/plans/2026-08-02-bridge-ui-ux-cheap-wins.md`; its freshness work is explicitly deferred to this structural lane.
- The correct baseline command is `uv run --extra dev python -m pytest -q`; it passes 736 tests in this checkout. Bare `uv run pytest -q` selected the wrong interpreter/entry point and is not a valid verification command here.
- Existing seams are `indexer.reindex`, `Store.transaction`/`Store._lock`, `api.create_app`, the nested SSE `_live_snapshot`/`_delta`, `cards.build_cards`/`cards.sort_key`, `dashboard.html`, `_card.html`, `live.js`, and the Node harness in `tests/test_static_js.py`.

## Shared Update Contract

Create `src/bridge/dashboard.py` with a JSON-serializable `DashboardUpdate` builder. Do not expose dataclasses directly to Jinja or JavaScript. The wire envelope is:

```json
{
  "schema": 1,
  "kind": "snapshot" | "patch",
  "generated_at": 0,
  "generation": 0,
  "refresh": {
    "attempted": false,
    "completed": true,
    "stats": null,
    "error": null
  },
  "freshness": {
    "server": "available" | "unavailable",
    "index_at": 0,
    "index_age_seconds": 0
  },
  "topbar": {
    "projects": 0,
    "running": 0,
    "queued": 0,
    "scheduled": 0,
    "today": 0,
    "last_5h": 0,
    "burn_rate": 0,
    "last_index": 0
  },
  "diagnostics": {"alert": false},
  "card_order": [0],
  "cards": {
    "0": {
      "live": {"available": true, "status": "idle", "started_at": 0, "model": null, "effort": null},
      "git": {"status": "ok", "branch": "main", "dirty_count": 0, "ahead": 0, "behind": 0, "oldest_uncommitted_at": null, "cached_at": null, "stale": false},
      "burn": {"today": 0, "last_5h": 0, "spark_points": ""}
    }
  },
  "unattributed": []
}
```

Contract rules:

- `kind=snapshot` contains all fields above. `kind=patch` contains only changed top-level fields, but any included field has the same shape and meaning; omitted fields mean “leave the existing DOM alone.” Both explicit refresh and SSE use this envelope, never a second ad-hoc response shape.
- `schema` is a literal integer `1`. Reject malformed/unknown envelopes in the browser without touching the DOM.
- `generation` is the monotonic last-successful-reindex generation. It increments only after a coordinator-owned reindex succeeds; failed attempts retain the prior generation and `freshness.index_at`. `freshness.index_at` is the last successful `index_runs.ran_at`; `index_age_seconds` is derived only from that timestamp. `generated_at` is envelope creation time for transport/debugging and is never evidence that indexed data is fresh.
- Transport health is client-local and separate from indexed-data freshness. The client tracks `connected`, `reconnecting`, `stale`, and `unavailable` using this precedence: server-declared unavailable or missing `index_at` → unavailable; successful `index_at` age >= 45 seconds → stale; otherwise EventSource error/connecting → reconnecting; otherwise connected. A liveness-only patch and an SSE heartbeat never update the indexed-data clock. A successful generation resets it from the new `freshness.index_at`.
- `topbar.today`, `topbar.last_5h`, and `topbar.burn_rate` are absolute integer counts/rates. The client may format them with the same kilo rules, but must never derive a percentage or gauge.
- `cards` is keyed by string project id because JSON object keys are strings. Card entries contain only fields that can be patched into existing markup; no handoff prompt, summary, textarea value, or card HTML is sent.
- `card_order` is the complete active project id list in server order from `cards.sort_key`: pinned first, then queued handoff, running, dirty/stale, recent, idle, with existing secondary recency/name ordering. The client compares this set with existing `[data-project-card]` nodes, reorders only the intersection, and never creates, clones, replaces, or removes a card. If the sets differ, it leaves the unmatched nodes in place and writes the persistent non-alarm message `Project list changed - reopen the panel to update cards.` No membership drift is converted to `refresh.error` or `freshness.server="unavailable"`; a successful reindex remains successful.
- `refresh.attempted=true` is used by `POST /api/refresh` and by a periodic update. A failed reindex keeps the last good store-derived card values, sets `freshness.server="unavailable"`, and carries the bounded exception text in `refresh.error`; it must not 500 the dashboard. Do not persist an exception traceback in the browser.

## Files and Responsibilities

- Create `src/bridge/dashboard.py`: snapshot projection, update envelope validation/projection, and the shared full/patch builders. It may call existing `cards.build_cards`, `cards.sort_key`, `store` readers, and the existing liveness/hook state; it must not write through `store.conn` directly.
- Create `src/bridge/refresh.py`: `RefreshCoordinator` with one process-local run lock, generation/status state, `run_once()`, immutable `status_snapshot()`, and `run_periodic()`. All explicit and periodic reindex calls go through this object. Do not add a condition/wait API; SSE uses its existing 3-second polling loop to read `status_snapshot()`.
- Modify `src/bridge/api.py`: accept an injected coordinator for tests, replace the stats-only refresh response with a full `DashboardUpdate`, make dashboard/SSE use the shared projection, and retain the existing liveness hysteresis and named SSE events.
- Modify `src/bridge/__main__.py`: construct one coordinator, start its periodic worker in the existing server process, stop/join it before closing the shared `Store`, and keep the scheduler thread’s current behavior intact.
- Modify `src/bridge/templates/base.html`, `dashboard.html`, and `_card.html`: add stable `data-*` patch hooks and an always-present freshness strip/Refresh action without changing card or textarea identity.
- Modify `src/bridge/static/live.js`: keep the existing EventSource logic, connection state machine, Refresh listener, and one shared `applyDashboardUpdate` path for POST and SSE in this file. Do not add a second front-end script or dependency.
- Modify `tests/test_api.py`, `tests/test_main.py`, `tests/test_static_js.py`; create `tests/test_dashboard.py` and `tests/test_refresh.py` if the new pure seams cannot be tested cleanly in existing modules.
- Create `tools/mutations/freshness-foundation.json`; every new contract must have a caught mutation, and `tests/test_mutation_specs.py` must continue to pass its anchor/name checks.

## Task 0: Track the approved plan before implementation

**Files:** `.agent/plans/2026-08-03-bridge-freshness-foundation.md`

This task is execution-gated. Do not run it during this planning revision. Before any production implementation begins, stage this plan and create exactly one planning commit with no attribution:

```bash
git add .agent/plans/2026-08-03-bridge-freshness-foundation.md
git commit -m "Plan the Bridge freshness foundation"
git status --short --branch --untracked-files=all
```

Expected: the commit succeeds, the exact message is used, and the final status is clean before Task 1 starts. Do not combine this commit with production files.

## Task 1: Define the snapshot projection and refresh coordinator

**Files:** `src/bridge/dashboard.py`, `src/bridge/refresh.py`, `tests/test_dashboard.py`, `tests/test_refresh.py`

**Interfaces:**

- `RefreshCoordinator(store: Store, cfg: Config, reindex_fn=reindex, interval_s=15.0)` owns refresh serialization. `run_once() -> RefreshResult` blocks behind the run lock; overlapping periodic/explicit calls cannot scan from the same stale `scan_state` concurrently. It uses only the existing `Store` methods and `indexer.reindex`, never a second database connection.
- `RefreshCoordinator.status_snapshot() -> RefreshStatus` returns an immutable narrow snapshot containing `generation`, last successful `index_at`, last attempt result, and bounded error text. `RefreshCoordinator.run_periodic(stop_event: threading.Event) -> None` performs one immediate attempt, then waits 15 seconds between attempts. Every exception is recorded as unavailable and logged; the loop survives until `stop_event` is set.
- `DashboardBuilder.full_update(kind="snapshot", refresh=...) -> dict` returns the envelope above from the existing `build_cards` projection and current `Store` reads. `DashboardBuilder.live_patch() -> dict` returns a `kind="patch"` envelope containing live card fields, `topbar.running`, unattributed sessions, diagnostics alert, and freshness metadata without reindexing.

- [ ] **Step 1: Write failing projection tests.** Seed two projects with a pinned queued handoff, a running project, dirty git states, tokens, a recorded index run, and an unavailable agent probe. Assert the exact absolute totals, diagnostics alert, card order, git fields, spark points, `generation`, `freshness.index_at`, and `freshness.index_age_seconds` fields. Assert `generated_at` is present only as envelope creation metadata, the live patch omits unchanged burn/git/order fields, and no update includes a handoff prompt.
- [ ] **Step 2: Write failing coordinator tests.** Use an injected `reindex_fn` with a barrier and counter. Assert two concurrent `run_once()` calls execute serially, only one successful generation is published per successful attempt, an exception preserves the prior success metadata while marking the result unavailable, `status_snapshot()` is immutable, and `run_periodic()` calls immediately then stops without another call after the event is set.
- [ ] **Step 3: Implement the pure projection and coordinator.** Keep the coordinator’s state behind its run lock and status lock; use the existing Store lock for SQLite operations; never hold either lock across `Event.wait()` or arbitrary probe sleep. Do not implement `wait_for_generation()`. Bound error text to a short single-line message. Keep index stats in `refresh.stats` so CLI/API callers do not lose existing observability.
- [ ] **Step 4: Run focused tests.** Run `uv run --extra dev python -m pytest -q tests/test_dashboard.py tests/test_refresh.py`. Expected: all new tests pass and the baseline remains untouched.
- [ ] **Step 5: Commit.** Use `git add src/bridge/dashboard.py src/bridge/refresh.py tests/test_dashboard.py tests/test_refresh.py && git commit -m "Add shared dashboard freshness contract"`; inspect the message for forbidden attribution before committing.

## Task 2: Run periodic reindexing in the server process safely

**Files:** `src/bridge/__main__.py`, `tests/test_main.py`, `tests/test_refresh.py`

- [ ] **Step 1: Add a failing lifecycle test.** Patch `uvicorn.run`, the coordinator’s `run_periodic`, and the existing scheduler. Assert `main(["serve"])` constructs one Store and one coordinator, starts the periodic worker in-process, passes that coordinator into `create_app`, sets the shared stop event, joins the periodic worker before closing Store, and still starts/stops the existing scheduled-run thread.
- [ ] **Step 2: Wire the worker.** Start a daemon thread for `RefreshCoordinator.run_periodic` beside the existing scheduler thread. Use the same `stop` event. Extend shutdown so Store closes only after both workers have stopped; if either worker remains alive after the bounded join, leave Store open as the current shutdown code does for a hung scheduler.
- [ ] **Step 3: Prove sole-writer/concurrency behavior.** Do not add a database thread, process, writable file, or direct `sqlite3.Connection` use. The coordinator serializes reindex runs; `Store._lock` remains the protection for FastAPI, scheduler, and refresh-worker access to the shared connection. Add a test that a periodic refresh and an explicit coordinator refresh cannot overlap their injected `reindex_fn` calls.
- [ ] **Step 4: Run focused verification.** Run `uv run --extra dev python -m pytest -q tests/test_main.py tests/test_refresh.py tests/test_store.py`; expected: scheduler lifecycle and all Store concurrency tests pass.
- [ ] **Step 5: Commit.** Use `git add src/bridge/__main__.py tests/test_main.py tests/test_refresh.py && git commit -m "Run periodic reindexing inside Bridge"`.

## Task 3: Make POST refresh and SSE use the same update envelope

**Files:** `src/bridge/api.py`, `tests/test_api.py`

- [ ] **Step 1: Add failing API tests.** Assert `POST /api/refresh` calls the injected coordinator, returns `schema=1`, `kind=snapshot`, full `topbar`, `cards`, `card_order`, `freshness`, and `refresh.stats`, and that the response contains changed store-derived values rather than stats only. Assert a coordinator failure returns a valid unavailable envelope with old card values rather than a 500. Assert a project-id set mismatch is still a successful response with available freshness and no refresh error.
- [ ] **Step 2: Add SSE contract tests.** Assert the first named `snapshot` event is the same full envelope shape returned by POST. Assert a liveness-only tick is a named `update` carrying `kind=patch`; assert a completed periodic generation causes a full `kind=snapshot` update; assert unchanged ticks emit no data frame and retain only the existing comment heartbeat. Keep `max_ticks`/`max_seconds` backstops so mutation tests cannot hang. Add the indexed-freshness sequence: hold the SSE transport open, emit liveness patches while `freshness.index_at` ages past 45 seconds, and assert the client state is stale; then emit a successful generation with a new `freshness.index_at` and assert it becomes connected/fresh again.
- [ ] **Step 3: Refactor the route seam.** Inject `RefreshCoordinator` into `create_app` with a test default. Replace the nested stats-only `/api/refresh` body with `coordinator.run_once()` followed by `DashboardBuilder.full_update(refresh=...)`. Make the SSE generator use the same builder: initial full update, compare the prior immutable `status_snapshot().generation` during the existing 3-second polling loop, send a full snapshot when the successful generation changes, send liveness patches otherwise, and send named `refresh` only for the existing capped-stream reconnect signal. Do not add `wait_for_generation()` or block the SSE generator on a condition.
- [ ] **Step 4: Preserve existing live guarantees.** Keep one liveness probe per dashboard snapshot, hook overlay, `LivenessDebouncer`, tombstones for ended sessions, malformed-frame resilience, no store lock during `time.sleep`, and no writes from `/events` itself. A periodic worker writes only through `reindex`; SSE remains a reader/projection path.
- [ ] **Step 5: Run focused verification.** Run `uv run --extra dev python -m pytest -q tests/test_api.py -k 'refresh or events or live or diagnostics'`; expected: all existing Phase 4 SSE tests plus the new contract tests pass.
- [ ] **Step 6: Commit.** Use `git add src/bridge/api.py tests/test_api.py && git commit -m "Return dashboard snapshots from refresh and SSE"`.

## Task 4: Add stable patch hooks without changing DOM identity

**Files:** `src/bridge/templates/base.html`, `src/bridge/templates/dashboard.html`, `src/bridge/templates/_card.html`, `tests/test_api.py`

- [ ] **Step 1: Add failing rendered-HTML tests.** Require an always-present `[data-freshness-strip]` with a visible text label, a labeled `[data-dashboard-refresh]` button, independent refresh and project-membership status live regions, and the initial `freshness.index_at`/successful generation attributes. Require `data-dashboard-total` hooks for all eight topbar totals, an always-present diagnostics alert hook, `[data-cards-list]`, and `data-project-card` ids.
- [ ] **Step 2: Add stable card leaves.** Keep each project `<li>` and each handoff `<textarea>` as the same server-rendered node. Add hooks for live parent/status/age/model/effort, git branch/dirty/ahead/behind/stale/cache/unavailable leaves, burn text, and sparkline `<polyline points>`. Render the live shell and diagnostics alert even when hidden/unavailable so a later update never needs to create a structural subtree.
- [ ] **Step 3: Encode initial freshness.** Put the initial `generated_at`, successful `generation`, `freshness.index_at`, and server availability on the freshness strip as attributes. Put the initial card membership/order on the existing list only as data attributes, plus an always-present non-alarm membership status node; do not put prompt text or handoff values into a new client-owned cache. The client must calculate indexed-data age from `index_at`, never from `generated_at`.
- [ ] **Step 4: Add structural assertions.** Assert both themes and current six-digit lowercase hex CSS tokens are unchanged by this lane, the 1400px grid rule remains unchanged, absolute token labels remain, every textarea count/value survives rendering, and no new `innerHTML`/reload hook appears in the live path.
- [ ] **Step 5: Run focused verification.** Run `uv run --extra dev python -m pytest -q tests/test_api.py -k 'dashboard or heading or textarea or token or card or diagnostics' tests/test_contrast.py`.
- [ ] **Step 6: Commit.** Use `git add src/bridge/templates/base.html src/bridge/templates/dashboard.html src/bridge/templates/_card.html tests/test_api.py && git commit -m "Add stable dashboard freshness patch hooks"`.

## Task 5: Apply snapshots, reorder existing cards, and expose connection freshness

**Files:** `src/bridge/static/live.js`, `tests/test_static_js.py`

**Client state machine and thresholds:**

- The SSE polling interval remains 3 seconds. The server refresh worker interval is 15 seconds. A page is `connected` after a valid update reports an available `freshness.index_at` younger than 45 seconds and the EventSource is not reconnecting; 45 seconds is the indexed-data stale threshold, deliberately separate from the 12-hour project dirty/stale threshold.
- `reconnecting` means EventSource reported an error/connecting state while indexed data is still younger than 45 seconds. `stale` means the last successful `freshness.index_at` is at least 45 seconds old, even if the EventSource remains open and liveness patches keep arriving. `unavailable` means the envelope says `freshness.server="unavailable"` or there is no successful `freshness.index_at`. The client compares this server indexed-data state with transport state using the precedence defined in the contract; `generated_at`, patch receipt, and heartbeat receipt never reset indexed freshness. These labels are mutually exclusive and rendered as words plus a glyph/state attribute, never color alone.
- A one-second timer may update the non-live age text, but the `role=status` label changes only when the four-state value changes. Heartbeat comment frames never call the announcer. Repeated connected updates do not announce “Connected” on every tick.

- [ ] **Step 1: Build a Node DOM harness before implementation.** Model real `li`, leaf, `polyline`, button, textarea, and persistent membership-status nodes with identity tokens, `classList`, `textContent`, `value`, `hidden`, and `append` behavior. Capture `fetch`, EventSource listeners, timer callbacks, and announcements.
- [ ] **Step 2: Add failing browser tests.** Prove a full snapshot patches topbar totals, git strip leaves, burn values, spark points, diagnostics visibility, live state, and existing card order. Prove patch updates only specified fields. Prove liveness patches do not advance the indexed-data clock and that an index age over 45 seconds yields stale even while transport frames continue. Prove a successful generation with a newer `index_at` returns to connected/fresh. Prove added and removed ids in `card_order` surface `Project list changed - reopen the panel to update cards.` without setting unavailable, while reordering only the intersection preserves every `<li>` and textarea object/value, including a user-edited value different from server-rendered text. Prove POST Refresh sends a request and applies its returned snapshot without reload.
- [ ] **Step 3: Add connection-state tests.** Drive connected → reconnecting → stale and server-unavailable transitions with a fake clock. Assert indexed freshness is calculated from `freshness.index_at`, not envelope `generated_at`, accepted liveness patches do not reset it, each transition is announced at most once, malformed frames are ignored, and heartbeat comments produce no announcement.
- [ ] **Step 4: Implement one `applyDashboardUpdate(update)` path.** Validate `schema`/`kind`; patch only existing selectors with `textContent`, `classList`, `hidden`, and `setAttribute`; set polyline `points`; compare the incoming `card_order` id set with existing card-node ids; append only existing intersection nodes in server order; and write the ASCII membership message on mismatch. Never query or assign `.value` on a handoff textarea, never use `innerHTML`, `replaceChildren`, `cloneNode`, `outerHTML`, `location.reload`, or an HTML fragment response. Never set `freshness.index_at` from a patch that lacks a successful generation.
- [ ] **Step 5: Implement the labeled Refresh action.** Disable the button during POST, show `Refreshing...` in its own status region, apply the returned full envelope on success, and show a bounded failure message while leaving all prompts/cards intact on a failed request. The action must call `POST /api/refresh`; it must not reload or issue a database-only endpoint.
- [ ] **Step 6: Run focused verification.** Run `uv run --extra dev python -m pytest -q tests/test_static_js.py tests/test_api.py -k 'refresh or live or textarea or dashboard'` and confirm Node is found by the absolute-path lookup under falsification.
- [ ] **Step 7: Commit.** Use `git add src/bridge/static/live.js tests/test_static_js.py && git commit -m "Patch dashboard freshness without replacing DOM"`.

## Task 6: Add mutation coverage and perform the final verification sweep

**Files:** `tools/mutations/freshness-foundation.json`, all files changed above as needed for exact anchors, `tests/test_mutation_specs.py`

Add one caught mutation for each of these failure classes, naming the exact focused test node in the JSON spec:

1. Remove the coordinator run lock or move the lock release before `reindex_fn`; `test_refresh_runs_do_not_overlap` must fail.
2. Skip the periodic worker’s immediate/interval `run_once`; `test_periodic_refresh_runs_and_increments_generation` must fail.
3. Return only `IndexStats` from `POST /api/refresh`; `test_post_refresh_returns_full_snapshot` must fail.
4. Emit a liveness patch after a generation change instead of a full snapshot; `test_sse_emits_full_update_after_periodic_generation` must fail.
5. Drop `card_order` application or sort client-side alphabetically; `test_snapshot_reorders_existing_cards_by_server_order` must fail.
6. Replace a card subtree or assign `innerHTML`; `test_dashboard_patch_preserves_card_and_textarea_identity` and the static source guard must fail.
7. Assign a server snapshot into a textarea’s `.value`; `test_dashboard_patch_preserves_user_edited_textarea_value` must fail.
8. Remove the POST Refresh fetch/apply path; `test_refresh_button_posts_and_applies_snapshot` must fail.
9. Reset indexed freshness from every accepted SSE frame, including a liveness patch; `test_liveness_patch_does_not_reset_index_freshness` must fail.
10. Use `generated_at` instead of `freshness.index_at` for the 45-second clock; `test_generated_at_is_not_index_freshness_clock` must fail.
11. Announce every accepted frame or omit the 45-second state gate; `test_connection_states_do_not_announce_heartbeats` and `test_stale_threshold_is_45_seconds` must fail.
12. Treat added/removed project ids as unavailable or replace/create/remove card nodes; `test_membership_drift_is_non_alarm_and_identity_safe` must fail.
13. Drop `freshness.server` error propagation or reuse the project’s 12-hour dirty threshold for dashboard freshness; `test_unavailable_snapshot_is_distinct_from_stale_project` must fail.

- [ ] **Step 1: Write the mutation JSON with exact final-file anchors.** Each mutation must declare `file`, `old`, `new`, `tests`, and `expect_count`; keep every anchor narrow enough for `tests/test_mutation_specs.py` to report drift instead of silently skipping later mutations.
- [ ] **Step 2: Run anchor/name checks.** Run `uv run --extra dev python -m pytest -q tests/test_mutation_specs.py`; expected: every new anchor matches exactly and every named test exists.
- [ ] **Step 3: Run the focused mutation spec from a committed clean implementation.** Run `uv run --extra dev python tools/falsify.py --spec tools/mutations/freshness-foundation.json`; expected: `13/13 mutations caught`. Falsification requires committed target files and restores them with git, so do not run it against uncommitted implementation changes.
- [ ] **Step 4: Run the complete suite and static checks.** Run `uv run --extra dev python -m pytest -q`, `git diff --check`, and `git status --short --untracked-files=all`. Expected: all tests pass, no whitespace errors, and only the intended implementation/plan files are present.
- [ ] **Step 5: Inspect the rendered contract manually.** Start the localhost server with a throwaway config/database, open the dashboard at narrow and wide viewport equivalents, edit two handoff textareas, click the labeled Refresh button, wait through one periodic cycle, and verify: totals/git/burn/diagnostics/order update; textarea DOM identity and typed values survive; one-column/two-column breakpoint remains 1400px; no reload occurs; connection labels distinguish connected, reconnecting, stale, and unavailable.

## Verification Commands for the Parent Orchestrator

```bash
git rev-parse HEAD
git status --short --branch --untracked-files=all
uv run --extra dev python -m pytest -q
uv run --extra dev python -m pytest -q tests/test_dashboard.py tests/test_refresh.py tests/test_api.py tests/test_static_js.py tests/test_main.py
uv run --extra dev python -m pytest -q tests/test_mutation_specs.py
uv run --extra dev python tools/falsify.py --spec tools/mutations/freshness-foundation.json
git diff --check
git status --short --untracked-files=all
```

Execution must stop before any scheduled-history/pagination work, manual theme, keyboard shortcuts, branch push, or production implementation outside this plan.
