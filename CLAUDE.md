# Bridge — notes for Claude Code

A local control panel for Claude Code work: one FastAPI process reads session
transcripts, git state and the live-session registry, stores the next-session
prompt ("handoff") for each project, and launches sessions from it. Usage is in
`README.md`; the design, and the invariants the tests defend, are in
`docs/ARCHITECTURE.md` — read the relevant section before a non-trivial change.

## Commands

```bash
uv sync --extra dev                         # set up (Python 3.13, uv)
make check                                  # THE gate: uv run --extra dev pytest -q
uv run --extra dev pytest -q tests/test_x.py -k name    # one slice while iterating
make mutate                                 # falsification harness over tools/mutations/
uv run tools/falsify.py --spec tools/mutations/<spec>.json   # one spec
uv run bridge serve                         # the panel, on 127.0.0.1:8787 (BRIDGE_PORT overrides)
uv run bridge index                         # reindex; defers to a running panel
```

- `make check` defines green, not a subset. Read its real exit code — never
  pipe it through `tail`/`head` (the shell is zsh, so no `PIPESTATUS` either).
- ~350 tests shell out to `node` and **skip silently** without it. A run
  without node on PATH is not a green gate. CI pins Node 20 and runs the suite
  under a throwaway `$HOME` to prove hermeticity.
- `falsify.py` restores targets with `git checkout`, so it only runs against
  **committed** files — commit before mutating, or it deletes your change.
- No linter or type checker is configured. Keep imports and locals clean by
  hand; `uvx ruff check --select F src tests tools` is a useful one-off probe.

## Layout

- `src/bridge/` — one module per job; `docs/ARCHITECTURE.md#modules` maps them.
  `api.py` is the app factory; write routes live in `routes_handoffs.py` /
  `routes_schedule.py`; `launcher.py` is the only thing that spawns.
- `src/bridge/templates/` + `src/bridge/static/` — Jinja and hand-written JS/CSS.
  No build step, no framework, no `node_modules`.
- `commands/handoff.md` — the `/handoff` slash command, shipped in the wheel.
- `tools/mutations/*.json` — mutation specs; `tools/falsify.py` runs them.
- `tests/` — pytest; `tests/js/minidom.js` is the DOM stand-in the JS tests use.

## Invariants that bite

**Data**
- The panel process is the **sole database writer**. The CLI talks HTTP and
  never opens the `Store`; `bridge index` opens it only when no panel answers.
  Don't add a second writer.
- Sessions, git state and tokens are a derived cache. **Handoffs and scheduled
  runs are authored** and exist only in the DB plus their on-disk journals
  (`spool.py`, `schedspool.py`). Journal before the DB write; a failure that
  would leave the journal behind the DB must not be swallowed.
- A journal record is keyed by id and **replaces** its predecessor. Build it from
  the whole row, never a hand-picked field list — a list silently drops every
  field added later.
- Replay (`rebuild_if_empty`) only ever runs into an empty table, and `serve`
  replays on boot, before the spool drain.
- Migrations are additive: a new column goes in `COLUMN_MIGRATIONS`, never a
  table rebuild, never a `CHECK` constraint. A new `Handoff` field has to reach
  `HandoffIn`, the CLI payload, and `Store.create_handoff`'s INSERT.

**Launching**
- **No test may spawn a real session.** `create_app` takes `launch_fn`,
  `launcher.launch` takes `run`, and the launcher tests put a fake `claude` on
  PATH. Never let a test reach `osascript` or a real `claude`.
- `launch()` claims the handoff before dispatching. Anything that can refuse a
  launch runs inside `_handed_back_on_refusal`, ahead of the `launches` row.
- `permission_mode` is a closed set, validated, and never persisted or
  pre-armed. `launcher.MODES` ≠ `SCHEDULABLE_MODES` (`exec` cannot be scheduled).

**Tests**
- Hermetic: autouse fixtures in `tests/conftest.py` point `$HOME` at a tmp dir,
  redirect the config file and the session registry to empty stand-ins, and
  fail loudly (a `BaseException`, so no catch-all can swallow it) on any write
  aimed at the real `~/.bridge`. A test that needs your own `~/.claude` data
  is a bug.
- `tests/test_handoff_command.py` executes the bash block in
  `commands/handoff.md` that it finds **by content**, asserting exactly one
  match. Keep that true when editing the file's fenced blocks.
- Green means nothing until a mutation has failed. A test written to defend a
  behaviour must be shown to fail against the broken version.
- `tests/test_mutation_specs.py` checks every spec anchor matches its file
  exactly `expect_count` times. When you move code under an anchor, re-point
  the anchor (lengthen it if it now matches twice) — don't drop the mutation.
  If the anchored code turns out to be dead, delete the code and its mutation.
- Before trusting a survivor verdict, check the named test actually **reaches**
  the mutated line. Injected doubles, fixture defaults, outer `try` blocks and
  hand-built JS stub DOMs are how a test ends up guarding a path production
  never takes. Make stub DOMs match the real template's nesting.

**Frontend**
- A persistent shell: `router.js` swaps `.shell__body` from a fragment fetch
  (`X-Bridge-Fragment: 1`). A route swaps only if it is in router.js's
  `SWAPPABLE`/`WORKSPACE_PATH`, its template extends `layout|default("base.html")`,
  and the route passes `"layout": _layout_for(request)`.
- Every script loads **once**. Per-page work registers on
  `window.bridgePage.onEnter` / `onLeave` / `onMorph`; no DOM node may be
  captured at module scope (`tests/test_shell_contract.py` enforces this).
- One SSE stream (`/events`). `live.js` patches the Overview in place;
  `liverefresh.js` morphs `/project/N`, `/schedule`, `/diagnostics` and
  `/settings`. The morph strips any attribute the server omits, so client-owned
  state (`open`, `hidden`, `aria-expanded`) needs `data-live-preserve` on the
  panel **and** its toggle. `/` and `/projects` are not morphed — adding them
  to the owned set breaks their client state.
- At ≥1024px `.shell__body`, not the window, is the scroll container.
- A status node inside `.shell__body` dies with every swap, including an
  in-place re-render (`bridgeNavigate(url, { push: false })`). Announce a change
  that re-renders into the shell's `[data-shell-announce]`, and pass `focus` so
  the router hands focus on instead of dropping it on `#main`.
- WCAG 2.2 AA. Status is never conveyed by colour alone; a control that
  removes itself moves focus first.

## Conventions

- Comments explain **why** — the constraint, the measurement, the failure the
  line prevents. This codebase comments densely in that register; match it and
  don't add comments that restate the code.
- Commits: imperative subject, a body that says why, one logical change each.
- `main` is protected (required `test` check, branch up to date, admins
  included) and PRs are rebase-merged. Work on a branch.
