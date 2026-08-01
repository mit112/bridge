# Bridge Phase 4 — Live Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`)
> syntax for tracking.

**Goal:** The panel stops being a snapshot. A card shows which sessions are *running right now*, what
a project's burn looks like over the week, and the last good git state with its age when a probe times
out — and it updates itself without a reload. Launching gains the two controls Mit asked for: a model
selector that names versions (Opus 5, Opus 4.8) rather than only aliases, and an opt-in
`--dangerously-skip-permissions`.

**Architecture:** Three data producers and three consumers, deliberately separated. A new `agents`
module wraps `claude agents --json` and is the *only* thing that knows liveness; `store` gains a
`git_cache` read/write pair and a 7-day token series; `indexer` records each run's stats so
diagnostics has something to read. On the consuming side `cards` grows two fields, the template grows
two bands, and one SSE endpoint pushes deltas that the client patches into the DOM. Every probe is
best-effort and degrades to a labelled `unavailable` — no probe failure may ever remove a card.

**Spec:** `docs/superpowers/specs/2026-07-31-bridge-control-panel-design.md` §3 (`agents`), §4
(`git_cache`), §6 (`GET /events`, `GET /api/diagnostics`), the card layout at lines 336–370, the
degradation table at lines 405–409, and the Phase 4 bullet at line 451 ("SSE, `agents` probe, window
meter, sparklines, diagnostics view").

**Builds on:** Phase 3, merged to `main` at `bc74b21` (297 tests, 61 mutations across
`tools/mutations/phase3-task*.json`, all caught). The `launches` table, the launch band, and the
`launcher` module are in place. Phase 4 reads what Phase 3 writes.

---

## Spec corrections measured this session

Phase 3 learned the hard way that this spec is a design document, not an observed one — it asserted
`--session-id` works with `--bg`, and it does not. The same check was run before writing this plan.
**Two of Phase 4's three probes are described incorrectly by the spec.** These are measured against
`claude` 2.1.220 on this machine, not inferred.

`claude agents --json` really returns:

```json
[
  {
    "pid": 19145,
    "cwd": "/Users/mitsheth/dev/bridge",
    "kind": "interactive",
    "startedAt": 1785548714710,
    "sessionId": "4000ea8d-43a4-4074-8d6f-3adccdb98f04",
    "name": "Built Bridge Phase 3 (the launcher) end to end on branch pha",
    "status": "busy"
  }
]
```

| Spec says | Reality | Consequence for this plan |
|---|---|---|
| fields `session_id`, `cwd`, `model`, `effort`, `started_at` | `sessionId`, `cwd`, `kind`, `startedAt`, `sessionId`, `name`, `status` | Keys are **camelCase**. Task 3 reads `sessionId`, not `session_id` |
| `model` and `effort` are available per live session | **Neither field exists at all** | The live band cannot show model/effort from the probe. It joins `sessions.model` / `launches.model` instead — Task 5 |
| `started_at` | `startedAt` in epoch **milliseconds** (`1785548714710`) | Divide by 1000. Everything else in this codebase is epoch seconds; mixing them silently renders "56 thousand years ago" |
| (not mentioned) | `status` ∈ `idle` \| `busy`, `kind` ∈ `interactive` \| `background`, plus `pid` and `name` | Richer than the spec's binary "running". Task 5 surfaces `busy` vs `idle` because it is free and it is the distinction that matters |
| `GET /api/diagnostics` reports "parse errors" | Nothing persists them; `indexer` returns per-run stats that the CLI prints and drops | Task 7 must add storage first. This is why diagnostics is a task and not a route |

Also verified, and load-bearing for Task 2: the flag Mit asked for is spelled
**`--dangerously-skip-permissions`** (there is no `--dangerously-accept-permissions`). A separate
`--allow-dangerously-skip-permissions` exists which only *makes the mode available* rather than
entering it; that is not what we want and must not be substituted.

`claude agents --json` exits 0 and needs no TTY, which is what makes it safe to call from the server.

---

## Decisions taken

Mit's two requests are (1) and (2) and are settled. The rest were taken under the standing
instruction to state an assumption and keep moving; each is cheap to reverse.

| # | Decision | Taken | Why | Reversal cost |
|---|---|---|---|---|
| 1 | Model selector detail | **A curated `(value, label)` catalog**: `opus` → "opus — latest (Opus 5)", `claude-opus-4-8` → "Opus 4.8", and so on. `value` is passed to `--model` verbatim | Mit's request. Aliases alone cannot express "pin me to 4.8". The label/value split is what lets the selector read as prose while the wire stays exact | One config list plus a template loop |
| 2 | Bypass permissions | **A per-launch opt-in**, off by default, never remembered. Checkbox in the band, `bypass_permissions` on the API, `--dangerously-skip-permissions` on the CLI | Mit's request. Not sticky, because a persisted default would silently apply a dangerous mode to a launch nobody was watching | One boolean threaded through four layers |
| 3 | Whether the model catalog moves to `~/.bridge/config.toml` | **No. It stays in `config.py`** | Out of scope per the Phase 3 handoff, and orthogonal: the catalog's *shape* changes here, and doing both at once would conflate a data-model change with a new file format | Additive later |
| 4 | What `git_cache` caches | **Only `status == "ok"` is written; only `status == "unavailable"` reads it back.** `not_a_repo` neither writes nor reads | `unavailable` is the only genuinely transient outcome (timeout, disk asleep). `not_a_repo` is stable truth — a deleted repo must be allowed to say so rather than showing a fossil | One branch in `build_cards` |
| 5 | What SSE pushes | **JSON deltas the client patches into specific nodes** — never a page reload, never an HTML fragment swap over the card | A reload or a whole-card swap would destroy an in-progress prompt edit in the handoff `<textarea>`, which is the one piece of state Bridge cannot rebuild. See the constraint below | Rewrite one JS file |
| 6 | Sparkline accessibility | **`aria-hidden="true"` on the SVG**, with the existing `23k today / 20k last 5h` text left as the accessible representation | The numbers are already there in text. A `role="img"` with a prose `<title>` would duplicate them for screen-reader users, and a 7-point polyline has no detail the text lacks | One attribute |
| 7 | Window meter (spec's Phase 4 bullet) | **Deferred.** Not in this plan | The 5h number is already on the card as text. A *meter* implies a denominator, and the enterprise plan's 5h window has no published token cap to divide by, so the bar would be decorative | n/a |

---

## Global Constraints

Phases 1–3 constraints all carry forward. Restated where Phase 4 can violate them, plus the ones this
phase introduces:

- **Bridge never writes to a user project repo**, and never mutates a session. Phase 4 adds three
  read-only probes and zero new writable locations.
- **Bridge launches sessions; it never hosts or supervises them.** `agents` is a *read*. Phase 4 must
  not gain the ability to kill, pause, or signal a session, however easy `pid` makes it look.
- **No probe failure may remove or blank a card.** Every one degrades to a labelled `unavailable`,
  matching `build_cards`'s existing `except Exception` around `probe_fn`.
- **Epoch seconds everywhere.** `startedAt` from `agents` is milliseconds and is the only place in the
  codebase that is; convert at the boundary, in `agents.py`, so nothing downstream has to know.
- **Additive migrations only.** New tables and columns via `SCHEMA` / `COLUMN_MIGRATIONS`; never a
  table rebuild.
- **Do not relocate `resolve_short_id`, `_iter_agent_dicts`, or `strip_ansi` out of `launcher.py`.**
  `tools/mutations/phase3-task3.json` anchors on their exact source text. `falsify.py` treats a
  zero-match `old` as a hard error, so a move fails loudly rather than silently — but it still breaks
  a committed spec. Task 3 therefore *imports* these from `launcher`; it does not move them. If a
  future phase does move them, update the spec in the same commit.
- **The SSE generator must never hold the store lock across a sleep.** `Store` is a single
  `sqlite3` connection guarded by one `RLock` (`store.py:151`). A stream that holds it while waiting
  freezes every other request in the panel. Acquire per poll, release before sleeping.
- **The handoff `<textarea>` is never re-rendered by a live update.** It is the only
  non-regenerable state in the system.
- **`--dangerously-skip-permissions` is emitted as a fixed literal**, never assembled from or
  influenced by user input, and only when the caller explicitly asked for it.
- **Tests never spawn a real `claude` and never sleep for real time.** The fake-shim-on-`PATH` idiom
  from `tests/test_launcher.py` (`FAKE_CLAUDE_SOURCE`) is the whole verification surface for
  `agents`; SSE timing is injected.
- **`tools/falsify.py` runs pytest with `PATH=/usr/bin:/bin`.** Any test shelling out to a
  non-builtin must resolve it by absolute path or it SKIPS, pytest exits 0, and the mutation reports
  SURVIVED against a test that is actually fine. This cost a debugging session in Phase 3
  (commit `f1eb092`).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/bridge/agents.py` | **new.** The only module that knows `claude agents --json`. Shape-tolerant parse, ms→s conversion, `unavailable` on any failure | 3 |
| `src/bridge/config.py` | `ModelChoice` dataclass; `DEFAULT_MODELS` becomes a catalog | 1 |
| `src/bridge/models.py` | `ModelChoice`-typed `Card.launch_models`; `GitState.cached_at`; new `LiveSession`, `AgentsState`; `LaunchSpec.bypass_permissions` | 1–3, 5 |
| `src/bridge/launcher.py` | `--dangerously-skip-permissions` in both argv builders | 2 |
| `src/bridge/store.py` | `put_git_cache` / `get_git_cache`; `token_series`; `index_runs` table + `record_index_run` / `latest_index_run` | 4, 6, 7 |
| `src/bridge/cards.py` | `RANK_RUNNING`; wire `live`, `spark`, cached-git fallback; `spark_points` pure function | 4–6 |
| `src/bridge/indexer.py` | Record each run's stats | 7 |
| `src/bridge/api.py` | `bypass_permissions` on `LaunchIn`; `GET /api/diagnostics`; `GET /events`; `GET /diagnostics` | 2, 7, 8 |
| `src/bridge/cli.py` | `--dangerously-skip-permissions` on `bridge launch` | 2 |
| `src/bridge/templates/_card.html` | Model catalog loop, bypass checkbox, live band, sparkline | 1, 2, 5, 6 |
| `src/bridge/templates/diagnostics.html` | **new.** The diagnostics view | 7 |
| `src/bridge/static/live.js` | **new.** `EventSource` subscription and targeted DOM patching | 8 |

---

### Task 1: The model catalog

**Files:** modify `src/bridge/config.py`, `src/bridge/models.py`, `src/bridge/cards.py`,
`src/bridge/templates/_card.html`; test `tests/test_cards.py`, `tests/test_api.py`

**Interfaces:**
```python
# Produces — consumed by Task 2's template edit and by cards.build_cards
@dataclass(frozen=True)
class ModelChoice:
    value: str   # passed to `--model` verbatim
    label: str   # shown in the selector

Config.models: list[ModelChoice]     # was list[str]
Card.launch_models: list[ModelChoice] # was list[str]
Config.efforts: list[str]             # UNCHANGED — effort has no versions to name
```

**Steps:**

- [ ] **Step 1: Write the failing test.** The catalog's values are what reach the wire, so assert on
      values, and assert the labels name versions:

```python
def test_the_model_catalog_offers_pinned_versions_and_latest_aliases():
    cfg = config.load()
    values = [m.value for m in cfg.models]
    assert "opus" in values          # latest-tracking alias
    assert "claude-opus-5" in values # pinned
    assert "claude-opus-4-8" in values
    labels = {m.value: m.label for m in cfg.models}
    assert labels["claude-opus-4-8"] == "Opus 4.8"
    assert "Opus 5" in labels["opus"]  # the alias says what it currently means


def test_every_catalog_value_is_unique():
    values = [m.value for m in config.load().models]
    assert len(values) == len(set(values))
```

- [ ] **Step 2: Run it and watch it fail.** `pytest tests/test_cards.py -k catalog -v` →
      `AttributeError: 'str' object has no attribute 'value'`.

- [ ] **Step 3: Implement.** In `config.py`, replacing `DEFAULT_MODELS = ["opus", "sonnet", "haiku"]`.
      Every id below was extracted from the installed CLI binary, not guessed:

```python
@dataclass(frozen=True)
class ModelChoice:
    """One entry in the launch band's model selector.

    `value` goes to `--model` verbatim and is never validated here: the CLI is
    the authority on what it accepts, exactly as with `effort`. `label` exists
    because "opus" alone cannot express "pin me to 4.8" — the alias floats to
    whatever is newest, which is the right default and the wrong record.
    """

    value: str
    label: str


# Ids verified against `claude` 2.1.220 on this machine. Aliases first (the
# common case, and the default selection), then pinned versions newest-first.
DEFAULT_MODELS = [
    ModelChoice("opus", "opus — latest (Opus 5)"),
    ModelChoice("claude-opus-5", "Opus 5"),
    ModelChoice("claude-opus-4-8", "Opus 4.8"),
    ModelChoice("claude-opus-4-7", "Opus 4.7"),
    ModelChoice("sonnet", "sonnet — latest (Sonnet 5)"),
    ModelChoice("claude-sonnet-5", "Sonnet 5"),
    ModelChoice("claude-sonnet-4-6", "Sonnet 4.6"),
    ModelChoice("haiku", "haiku — latest (Haiku 4.5)"),
    ModelChoice("claude-haiku-4-5", "Haiku 4.5"),
    ModelChoice("fable", "fable — latest (Fable 5)"),
    ModelChoice("claude-fable-5", "Fable 5"),
]
```

- [ ] **Step 4: Update the template.** The suggested-model prepend logic currently compares a string
      against a list of strings and must now compare against values. A suggestion the catalog does not
      contain is still prepended rather than dropped — that behaviour is already tested and must
      survive:

```jinja
{% set model_values = card.launch_models | map(attribute="value") | list %}
{% set model_options = ([ModelChoice(sm, sm)] if sm and sm not in model_values else []) + card.launch_models %}
...
{%- for m in model_options %}
<option value="{{ m.value }}"{% if m.value == sm or (not sm and loop.first) %} selected{% endif %}>{{ m.label }}</option>
{%- endfor %}
```

      Constructing a `ModelChoice` inside Jinja needs it exposed to the environment. Do **not** do
      that — pass a ready-made list instead. Add to `cards.build_cards`, which already copies the
      config onto the card:

```python
def model_options(catalog: list[ModelChoice], suggested: str | None) -> list[ModelChoice]:
    """The catalog, with an off-catalog suggestion prepended labelled as itself.

    Silently launching a different model than the last session used is worse
    than showing an unfamiliar value, so an unknown suggestion is surfaced
    rather than dropped.
    """
    if suggested and suggested not in [m.value for m in catalog]:
        return [ModelChoice(suggested, suggested), *catalog]
    return list(catalog)
```

      and set `launch_models=model_options(cfg.models, (handoff or {}).get("suggested_model"))` so the
      template only loops. The template's `{% set model_options = ... %}` block disappears entirely.

- [ ] **Step 5: Run the suite.** `pytest -q`. Expect failures in any test asserting
      `cfg.models == ["opus", ...]` or scraping `<option>opus</option>`; update them to the new shape.
      `tests/test_contrast.py` renders the template, so a Jinja error surfaces there too.

- [ ] **Step 6: Write the mutation spec** `tools/mutations/phase4-task1.json` and run it.

- [ ] **Step 7: Commit.**

```bash
git add src/bridge/config.py src/bridge/models.py src/bridge/cards.py \
        src/bridge/templates/_card.html tests/ tools/mutations/phase4-task1.json
git commit -m "Name model versions in the launch band's selector"
```

**Tests (falsification required):**
- [ ] An off-catalog `suggested_model` is prepended and selected. *Mutation: drop the prepend branch →
      the suggestion vanishes and the band silently offers a different model.*
- [ ] With no suggestion, the first catalog entry is selected. *Mutation: remove `loop.first` →
      nothing is selected and the browser's implicit first-option choice becomes invisible.*
- [ ] `<option value>` carries `value` while the text carries `label`. *Mutation: emit `m.label` as
      the value → `--model "Opus 4.8"` reaches the CLI and the launch fails.*

---

### Task 2: Bypass-permissions launches

**Files:** modify `src/bridge/launcher.py`, `src/bridge/api.py`, `src/bridge/cli.py`,
`src/bridge/templates/_card.html`, `src/bridge/static/launch.js`; test `tests/test_launcher.py`,
`tests/test_api.py`, `tests/test_cli.py`, `tests/test_static_js.py`

**Interfaces:**
```python
LaunchSpec.bypass_permissions: bool = False   # consumed by both argv builders
LaunchIn.bypass_permissions: bool = False     # POST /api/launch
BYPASS_FLAG = "--dangerously-skip-permissions"
```

**Steps:**

- [ ] **Step 1: Write the failing tests.** Both modes, and — most importantly — the default:

```python
def test_terminal_mode_omits_the_bypass_flag_by_default():
    spec = launcher.LaunchSpec(project_path="/p", prompt="hi")
    assert "dangerously" not in launcher.build_shell_command(spec, "/tmp/p.txt", claude="/bin/claude")


def test_terminal_mode_emits_the_bypass_flag_when_asked():
    spec = launcher.LaunchSpec(project_path="/p", prompt="hi", bypass_permissions=True)
    cmd = launcher.build_shell_command(spec, "/tmp/p.txt", claude="/bin/claude")
    assert " --dangerously-skip-permissions " in cmd
    # Unquoted and unvalued: it is a fixed literal, so quoting it would be noise
    # that a future reader might "fix" by making it dynamic.
    assert "'--dangerously-skip-permissions'" not in cmd


def test_background_mode_passes_the_bypass_flag_as_its_own_argv_element():
    spec = launcher.LaunchSpec(project_path="/p", prompt="hi", mode="background",
                               bypass_permissions=True)
    argv = launcher.build_bg_argv(spec, claude="/bin/claude")
    assert argv.count("--dangerously-skip-permissions") == 1
    assert argv[-1] == "hi"  # still exactly one element, still last


def test_the_bypass_flag_is_never_the_allow_variant():
    """`--allow-dangerously-skip-permissions` only makes the mode AVAILABLE.

    Substituting it would produce a launch that looks bypassed and is not.
    """
    spec = launcher.LaunchSpec(project_path="/p", prompt="hi", bypass_permissions=True)
    argv = launcher.build_bg_argv(spec, claude="/bin/claude")
    assert "--allow-dangerously-skip-permissions" not in argv
```

- [ ] **Step 2: Run them and watch them fail.** `TypeError: unexpected keyword argument
      'bypass_permissions'`.

- [ ] **Step 3: Implement.** Add the field to `LaunchSpec`, then in `build_shell_command` after the
      effort branch and before `-n`:

```python
    # A fixed literal, deliberately unquoted and never assembled from input.
    # Placed before `-n` so the prompt stays last in every shape this builds.
    if spec.bypass_permissions:
        argv.append(BYPASS_FLAG)
```

      and the identical branch in `build_bg_argv` (`argv.append(BYPASS_FLAG)`).

- [ ] **Step 4: Thread it through the API and CLI.** `LaunchIn.bypass_permissions: bool = False`,
      passed into the `LaunchSpec` in `post_launch`. On the CLI,
      `--dangerously-skip-permissions` as `action="store_true"`, sent in the JSON body. Name the CLI
      flag exactly as `claude` spells it so muscle memory transfers.

- [ ] **Step 5: The checkbox.** In the launch band, after the effort field. Unchecked always — it is
      never pre-checked from a suggestion, because no handoff should be able to arm it:

```jinja
    <span class="launch__field launch__field--danger">
      <input class="launch__check" type="checkbox" id="{{ lid }}-bypass"
             data-launch-bypass="{{ lid }}">
      <label class="launch__label" for="{{ lid }}-bypass"
             title="Runs the session with --dangerously-skip-permissions: it will not ask before editing files or running commands">
        Skip permissions
      </label>
    </span>
```

      and in `launch.js`, alongside the existing model/effort reads:

```js
  const bypass = document.querySelector(`[data-launch-bypass="${id}"]`);
  body.bypass_permissions = bypass ? bypass.checked : false;
```

- [ ] **Step 6: Style the affordance so it does not read as ordinary.** In `app.css`, give
      `.launch__field--danger` the existing warning accent used for the risk band — colour plus the
      word "Skip permissions", never colour alone. `tests/test_contrast.py` already enforces contrast
      ratios; add the new class to whatever it iterates so AA is checked rather than assumed.

- [ ] **Step 7: Run the suite, write** `tools/mutations/phase4-task2.json`**, run it, commit.**

```bash
git commit -m "Offer an opt-in bypass-permissions launch"
```

**Tests (falsification required):**
- [ ] Default off in both modes. *Mutation: default `bypass_permissions=True` → every launch silently
      becomes a bypassed one. This is the mutation that matters most in the whole phase.*
- [ ] The flag appears exactly once when asked. *Mutation: append unconditionally → the checkbox stops
      meaning anything.*
- [ ] The prompt is still the last argv element in background mode. *Mutation: append the flag after
      the prompt → `claude` reads the flag as part of the prompt.*
- [ ] `tests/test_static_js.py` asserts `launch.js` sends `bypass_permissions`, executing the file
      with **node resolved by absolute path**. *Mutation: drop the body key → the server always
      defaults to false and the checkbox is decorative.*

---

### Task 3: The `agents` probe

**Files:** create `src/bridge/agents.py`; modify `src/bridge/models.py`; test
`tests/test_agents.py`

**Interfaces:**
```python
@dataclass(frozen=True)
class LiveSession:
    session_id: str
    cwd: str
    kind: str          # interactive | background
    status: str        # idle | busy
    name: str | None
    started_at: int    # epoch SECONDS, converted from the probe's milliseconds

@dataclass(frozen=True)
class AgentsState:
    status: str                      # ok | unavailable
    sessions: list[LiveSession] = ()

def probe(claude: str | None = None, run=subprocess.run, timeout: float = 2.0) -> AgentsState
def by_project(state: AgentsState, alias_map: dict[str, str]) -> dict[str, list[LiveSession]]
```

**Steps:**

- [ ] **Step 1: Write the failing tests** against the real recorded payload. Paste the measured JSON
      verbatim as a fixture — a hand-simplified one would drift from reality, which is precisely how
      the spec got this wrong:

```python
REAL_PAYLOAD = """[
  {"pid": 10210, "cwd": "/Users/mitsheth/dev/projectY", "kind": "interactive",
   "startedAt": 1785536395229, "sessionId": "eab23eb4-4734-4d73-99f9-f039bb891c51",
   "name": "projecty-80", "status": "idle"},
  {"pid": 19145, "cwd": "/Users/mitsheth/dev/bridge", "kind": "interactive",
   "startedAt": 1785548714710, "sessionId": "4000ea8d-43a4-4074-8d6f-3adccdb98f04",
   "name": "Built Bridge Phase 3", "status": "busy"}
]"""


def test_probe_reads_camelcase_keys_and_converts_milliseconds():
    state = agents.probe(claude="/bin/claude", run=fake_run(0, REAL_PAYLOAD))
    assert state.status == "ok"
    live = {s.session_id: s for s in state.sessions}
    s = live["4000ea8d-43a4-4074-8d6f-3adccdb98f04"]
    assert s.status == "busy"
    assert s.kind == "interactive"
    # 1785548714710 ms -> 1785548714 s. A missing //1000 lands in the year 58,000.
    assert s.started_at == 1785548714


def test_a_nonzero_exit_is_unavailable_not_empty():
    """`unavailable` and "nothing running" must not be the same value.

    An empty list would render "no live sessions", asserting something the
    probe did not learn.
    """
    state = agents.probe(claude="/bin/claude", run=fake_run(1, ""))
    assert state.status == "unavailable"
    assert state.sessions == []


@pytest.mark.parametrize("payload", ["not json", "", "null", '{"unexpected": "shape"}', "[[]]"])
def test_malformed_output_is_unavailable(payload):
    assert agents.probe(claude="/bin/claude", run=fake_run(0, payload)).status == "unavailable"


def test_an_entry_missing_its_session_id_is_skipped_not_fatal():
    payload = '[{"cwd": "/p"}, ' + REAL_PAYLOAD[1:]
    state = agents.probe(claude="/bin/claude", run=fake_run(0, payload))
    assert state.status == "ok"
    assert len(state.sessions) == 2  # the junk entry dropped, the good ones kept


def test_a_timeout_is_unavailable():
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=2.0)
    assert agents.probe(claude="/bin/claude", run=boom).status == "unavailable"


def test_by_project_maps_an_aliased_cwd_to_its_canonical_project():
    """A session started under an old path belongs to the canonical project."""
    state = AgentsState(status="ok", sessions=[
        LiveSession("a" * 8 + "-0000-0000-0000-000000000000",
                    "/Users/mitsheth/Documents/projectX", "interactive", "busy", None, 1)])
    grouped = agents.by_project(state, {"/Users/mitsheth/Documents/projectX": "/Users/mitsheth/dev/projectX"})
    assert "/Users/mitsheth/dev/projectX" in grouped
```

- [ ] **Step 2: Run them and watch them fail.** `ModuleNotFoundError: bridge.agents`.

- [ ] **Step 3: Implement.** Reuse `launcher`'s shape-tolerant iterator and ANSI stripper rather than
      re-deriving them — and note *why* the import points that way:

```python
"""Read-only probe of `claude agents --json`. Never signals a session.

Verified against `claude` 2.1.220, because the spec describes this wrongly:
keys are camelCase (`sessionId`, `startedAt`), `startedAt` is epoch
MILLISECONDS, and there is no `model` or `effort` field at all. The live band
therefore takes model/effort from our own tables, not from here.

`_iter_agent_dicts` and `strip_ansi` are imported from `launcher` rather than
moved here, even though this is their natural home:
`tools/mutations/phase3-task3.json` anchors on their exact source text in
`launcher.py`, and moving them would break a committed mutation spec.
"""

import json
import subprocess

from bridge.launcher import SESSION_ID_RE, _iter_agent_dicts, resolve_claude, strip_ansi
from bridge.models import AgentsState, LiveSession

UNAVAILABLE = AgentsState(status="unavailable", sessions=[])


def probe(claude=None, run=subprocess.run, timeout: float = 2.0) -> AgentsState:
    try:
        claude = claude or resolve_claude()
        proc = run([claude, "agents", "--json"], capture_output=True, text=True,
                   timeout=timeout)
        if proc.returncode != 0:
            return UNAVAILABLE
        data = json.loads(strip_ansi(proc.stdout or ""))
    except Exception:  # noqa: BLE001 - every failure is the same `unavailable`
        return UNAVAILABLE

    sessions = []
    for entry in _iter_agent_dicts(data):
        session_id = str(entry.get("sessionId") or "").lower()
        if not SESSION_ID_RE.match(session_id):
            continue  # no id, no correlation: drop the entry, keep the rest
        started = entry.get("startedAt")
        sessions.append(LiveSession(
            session_id=session_id,
            cwd=str(entry.get("cwd") or ""),
            kind=str(entry.get("kind") or "interactive"),
            status=str(entry.get("status") or "idle"),
            name=entry.get("name") or None,
            # Milliseconds -> seconds. The ONLY ms value in this codebase.
            started_at=int(started) // 1000 if isinstance(started, (int, float)) else 0,
        ))
    return AgentsState(status="ok", sessions=sessions)
```

      `json.loads("null")` returns `None`, and `_iter_agent_dicts(None)` yields nothing — which would
      make a `null` payload look like `ok` with no sessions. Reject a non-list, non-dict payload
      explicitly so `null` is `unavailable`.

- [ ] **Step 4: `by_project`.** Map each `cwd` through the alias table, then group. A `cwd` with no
      alias maps to itself.

- [ ] **Step 5: Run, write** `tools/mutations/phase4-task3.json`**, run it, commit.**

```bash
git commit -m "Probe claude agents for live sessions"
```

**Tests (falsification required):**
- [ ] ms→s conversion. *Mutation: drop `// 1000` → every live session claims to have started 56,000
      years in the future and "running for" arithmetic goes negative.*
- [ ] Non-zero exit is `unavailable`, not empty. *Mutation: return `AgentsState("ok", [])` → the panel
      asserts "nothing is running" on the strength of a failed probe.*
- [ ] `null` payload is `unavailable`. *Mutation: accept any parse → same false claim.*
- [ ] An entry with no `sessionId` is skipped, not fatal. *Mutation: drop the regex guard → one junk
      entry makes the whole probe unavailable.*
- [ ] Reads `sessionId`, not `session_id`. *Mutation: read `session_id` → the probe silently returns
      zero sessions against real output, which is the exact bug the spec would have caused.*

---

### Task 4: Last good git state, with its age

**Files:** modify `src/bridge/store.py`, `src/bridge/models.py`, `src/bridge/cards.py`,
`src/bridge/templates/_card.html`; test `tests/test_store.py`, `tests/test_cards.py`

**Interfaces:**
```python
GitState.cached_at: int | None = None   # set ONLY on a cache hit; None means live
Store.put_git_cache(project_id: int, git: GitState, probed_at: int) -> None
Store.get_git_cache(project_id: int) -> tuple[GitState, int] | None   # (state, probed_at)
```

**Steps:**

- [ ] **Step 1: Write the failing tests.** The behaviour is a fallback, so the test is a sequence:

```python
def test_a_timed_out_probe_shows_the_last_good_state_with_its_age(store, cfg):
    pid = store.upsert_project("/p", "p")
    good = GitState(status="ok", branch="main", dirty_count=2)
    cards.build_cards(store, cfg, probe_fn=lambda p: good)      # populates the cache

    card = cards.build_cards(store, cfg, probe_fn=lambda p: GitState(status="unavailable"))[0]
    assert card.git.status == "ok"
    assert card.git.branch == "main"
    assert card.git.cached_at is not None   # and the template renders its age


def test_an_unavailable_probe_never_overwrites_the_cache(store, cfg):
    """Otherwise the first timeout destroys the very state it should fall back to."""
    good = GitState(status="ok", branch="main")
    cards.build_cards(store, cfg, probe_fn=lambda p: good)
    cards.build_cards(store, cfg, probe_fn=lambda p: GitState(status="unavailable"))
    cards.build_cards(store, cfg, probe_fn=lambda p: GitState(status="unavailable"))
    state, _ = store.get_git_cache(1)
    assert state.branch == "main"


def test_not_a_repo_is_reported_not_papered_over(store, cfg):
    """A deleted repo must be allowed to say so rather than showing a fossil."""
    cards.build_cards(store, cfg, probe_fn=lambda p: GitState(status="ok", branch="main"))
    card = cards.build_cards(store, cfg, probe_fn=lambda p: GitState(status="not_a_repo"))[0]
    assert card.git.status == "not_a_repo"


def test_a_cache_payload_from_another_version_does_not_crash(store):
    """The cache is JSON on disk; a field we no longer have must not raise."""
    store.conn.execute(
        "INSERT INTO git_cache (project_id, payload_json, probed_at) VALUES (1, ?, 1)",
        ('{"status": "ok", "branch": "main", "a_field_from_the_future": 7}',))
    state, _ = store.get_git_cache(1)
    assert state.branch == "main"


def test_no_cache_and_an_unavailable_probe_stays_unavailable(store, cfg):
    card = cards.build_cards(store, cfg, probe_fn=lambda p: GitState(status="unavailable"))[0]
    assert card.git.status == "unavailable"
    assert card.git.cached_at is None
```

- [ ] **Step 2: Run and watch fail.** `AttributeError: 'Store' has no attribute 'put_git_cache'`.

- [ ] **Step 3: Implement the store pair.** The table already exists in `SCHEMA` — Phase 1 created it
      and nothing ever read or wrote it, which is what this task fixes:

```python
    def put_git_cache(self, project_id: int, git: GitState, probed_at: int) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO git_cache (project_id, payload_json, probed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET payload_json=excluded.payload_json, "
                "probed_at=excluded.probed_at",
                (project_id, json.dumps(dataclasses.asdict(git)), probed_at))

    def get_git_cache(self, project_id: int) -> tuple[GitState, int] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT payload_json, probed_at FROM git_cache WHERE project_id=?",
                (project_id,)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            return None
        # Filter to fields GitState actually has. The cache is a JSON blob that
        # outlives any one version of the dataclass, so an added or removed
        # field must degrade rather than raise.
        known = {f.name for f in dataclasses.fields(GitState)}
        return GitState(**{k: v for k, v in payload.items() if k in known}), row["probed_at"]
```

- [ ] **Step 4: Wire the fallback into `build_cards`,** replacing the bare `git = probe_fn(...)`:

```python
        if git.status == "ok":
            store.put_git_cache(row["id"], git, now)
        elif git.status == "unavailable":
            # Only `unavailable` is transient. `not_a_repo` is stable truth and
            # falls through untouched, so a deleted repo reports honestly.
            cached = store.get_git_cache(row["id"])
            if cached is not None:
                git, probed_at = cached
                git = replace(git, cached_at=probed_at)
```

      `cached_at` must be excluded when *writing* (a live state has none) — `asdict` will include it
      as `None`, which round-trips correctly, so no special case is needed. Confirm that with the
      version-drift test above.

- [ ] **Step 5: Render the age.** In the git line of `_card.html`, only when `cached_at` is set:

```jinja
      {% if card.git.cached_at %}
        <span class="muted" title="The git probe timed out; this is the last state Bridge saw">
          · as of {{ card.git.cached_at | ago_epoch }} ago
        </span>
      {% endif %}
```

      **`ago_epoch`, not `ago`.** `api.py:119-120` registers both: `ago` takes an ISO-8601 string
      (`card.session.ended_at`), `ago_epoch` takes an epoch int. `cached_at` is an epoch int, and
      passing it to `ago` would either raise or render nonsense. Both filters emit a bare duration,
      so the word "ago" is supplied by the template — as every existing call site does.

- [ ] **Step 6: Run, write** `tools/mutations/phase4-task4.json`**, run it, commit.**

```bash
git commit -m "Show the last good git state with its age when a probe fails"
```

**Tests (falsification required):**
- [ ] The fallback fires. *Mutation: delete the `elif` branch → back to today's behaviour, "git
      unavailable" on a card whose state is known.*
- [ ] `unavailable` never overwrites. *Mutation: cache unconditionally → the first timeout overwrites
      the good state with the failure, so the fallback can never work again. This is the subtle one.*
- [ ] `not_a_repo` passes through. *Mutation: treat it as transient → a deleted repo shows a fossil
      branch forever.*
- [ ] Unknown payload fields are filtered. *Mutation: `GitState(**payload)` → a schema change turns
      every cache hit into a `TypeError` and takes the whole dashboard down.*

---

### Task 5: The live band, and the running rank

**Files:** modify `src/bridge/models.py`, `src/bridge/cards.py`, `src/bridge/api.py`,
`src/bridge/templates/_card.html`; test `tests/test_cards.py`, `tests/test_api.py`

**Interfaces:**
```python
Card.live: LiveSession | None = None
Card.live_unavailable: bool = False   # the probe failed, as distinct from nothing running
cards.build_cards(store, cfg, probe_fn=None, agents_fn=None)   # agents_fn late-bound, like probe_fn

RANK_HANDOFF = -1   # unchanged
RANK_RUNNING = 0    # new
RANK_STALE  = 1     # was 0
RANK_RECENT = 2     # was 1
RANK_OTHER  = 3     # was 2
```

**Steps:**

- [ ] **Step 1: Write the failing tests.** The ordering is the spec's, and it is not the obvious one —
      a queued handoff outranks a running session:

```python
def test_a_running_project_sorts_above_dirty_and_stale_but_below_a_queued_handoff():
    """Spec order: queued handoff -> running now -> dirty and stale -> recent -> idle.

    A handoff outranks a running session because a running session needs
    nothing from you; a queued one is waiting on you.
    """
    handoff, running, stale = make_cards(...)
    assert [c.name for c in sorted([stale, running, handoff], key=cards.sort_key)] == \
           [handoff.name, running.name, stale.name]


def test_a_busy_session_is_distinguished_from_an_idle_one(store, cfg):
    card = build_with_live(status="busy")
    assert card.live.status == "busy"


def test_a_failed_agents_probe_leaves_cards_intact_and_says_unavailable(store, cfg):
    cards_ = cards.build_cards(store, cfg, agents_fn=lambda: AgentsState("unavailable", []))
    assert len(cards_) == 1              # the card is NOT removed
    assert cards_[0].live is None
    assert cards_[0].live_unavailable is True


def test_a_raising_agents_probe_cannot_take_down_the_dashboard(store, cfg):
    def boom():
        raise RuntimeError("probe exploded")
    assert len(cards.build_cards(store, cfg, agents_fn=boom)) == 1


def test_the_agents_probe_runs_once_for_the_whole_dashboard(store, cfg):
    """Not once per card: 30 projects must not mean 30 subprocesses."""
    calls = []
    cards.build_cards(store, cfg, agents_fn=lambda: (calls.append(1), AgentsState("ok", []))[1])
    assert len(calls) == 1
```

- [ ] **Step 2: Run and watch fail.** `TypeError: unexpected keyword argument 'agents_fn'`.

- [ ] **Step 3: Implement.** One probe for the whole build, grouped by project path before the loop —
      the per-card alternative would fork a subprocess per project:

```python
    if agents_fn is None:
        agents_fn = agents.probe
    try:
        live_state = agents_fn()
    except Exception:  # noqa: BLE001 - matching probe_fn: a broken probe hides no cards
        live_state = AgentsState(status="unavailable", sessions=[])
    live_by_path = agents.by_project(live_state, store.alias_map())
```

      then per card, `live = (live_by_path.get(row["path"]) or [None])[0]` — most recently started
      first if several — and `live_unavailable = live_state.status == "unavailable"`. Renumber the
      ranks and add the `RANK_RUNNING` branch to `sort_key`, keeping the handoff branch first.

- [ ] **Step 4: Render the band.** Above the git line, colour plus a word — never colour alone:

```jinja
  {% if card.live %}
    <p class="live live--{{ card.live.status }}">
      <span class="live__dot" aria-hidden="true">●</span>
      <span>{{ "running" if card.live.status == "busy" else "idle" }}</span>
      <span>· {{ card.live.started_at | ago_epoch }}</span>
      {# model/effort come from OUR tables: `claude agents --json` has neither #}
      {% if card.session and card.session.model %}
        <span>· {{ card.session.model }}{% if card.session.effort %}/{{ card.session.effort }}{% endif %}</span>
      {% endif %}
    </p>
  {% elif card.live_unavailable %}
    <p class="live live--unknown"><span>live status unavailable</span></p>
  {% endif %}
```

- [ ] **Step 5: Update the `cards.py` docstring.** It currently promises "Phase 4 will add running
      sessions above rank 0 by shifting these values". Phase 4 is now, so state what happened instead
      of what will.

- [ ] **Step 6: Run, write** `tools/mutations/phase4-task5.json`**, run it, commit.**

```bash
git commit -m "Show live sessions on the card and rank them"
```

**Tests (falsification required):**
- [ ] Handoff still outranks running. *Mutation: `RANK_RUNNING = -2` → a running session buries the
      card that is actually waiting on you, inverting the panel's whole purpose.*
- [ ] A failed probe keeps every card. *Mutation: `continue` on unavailable → the dashboard empties
      when `claude` is missing.*
- [ ] `live_unavailable` is distinct from `live is None`. *Mutation: collapse the two → "nothing
      running" is asserted from a failed probe.*
- [ ] One probe per build. *Mutation: move the probe inside the loop → N subprocesses per page load;
      the test counts calls, so this is caught rather than merely slow.*

---

### Task 6: Sparklines

**Files:** modify `src/bridge/store.py`, `src/bridge/cards.py`,
`src/bridge/templates/_card.html`; test `tests/test_store.py`, `tests/test_cards.py`

**Interfaces:**
```python
Store.token_series(project_id: int, days: int, now: int) -> list[int]  # oldest first, len == days
cards.spark_points(values: list[int], width: int = 72, height: int = 20) -> str  # "x,y x,y ..."
```

**Steps:**

- [ ] **Step 1: Write the failing tests.** The geometry is pure, so it is cheap to pin exactly — and
      the degenerate cases are where sparklines actually break:

```python
def test_token_series_returns_one_bucket_per_day_oldest_first(store):
    # two sessions today, one three days ago
    ...
    series = store.token_series(pid, days=7, now=NOW)
    assert len(series) == 7
    assert series[-1] == 300   # today is last
    assert series[3] == 50     # three days ago
    assert series[0] == 0      # no activity that day, present as a zero


def test_all_zeros_is_a_flat_baseline_not_a_division_by_zero():
    """A project with no burn is the common case for an idle card."""
    points = cards.spark_points([0] * 7)
    ys = {p.split(",")[1] for p in points.split()}
    assert len(ys) == 1        # one flat line
    assert float(ys.pop()) == 20.0   # at the baseline, not through the roof


def test_a_single_flat_nonzero_series_does_not_divide_by_zero():
    assert cards.spark_points([5] * 7)   # max == min


def test_the_peak_touches_the_top_and_the_trough_the_bottom():
    points = [p.split(",") for p in cards.spark_points([0, 10]).split()]
    assert float(points[0][1]) == 20.0   # SVG y grows downward: trough is y=height
    assert float(points[1][1]) == 0.0    # peak is y=0


def test_an_empty_series_produces_no_points_rather_than_raising():
    assert cards.spark_points([]) == ""


def test_the_series_uses_the_same_token_definition_as_the_burn_text(store):
    """The sparkline sits inches from `token_totals`'s "23k today".

    `token_totals` sums tokens_in + tokens_out only. A series that also summed
    the cache columns would draw a line whose shape contradicts the number
    printed beside it, and nothing would ever flag it.
    """
    pid = make_session(store, tokens_in=10, tokens_out=5,
                       tokens_cache_create=1000, tokens_cache_read=2000, ended=NOW)
    assert store.token_series(pid, days=1, now=NOW)[-1] == store.token_totals(pid, NOW - 86400)
```

- [ ] **Step 2: Run and watch fail.** `AttributeError: no attribute 'token_series'`.

- [ ] **Step 3: Implement the query.** Bucket by day in SQL, then fill gaps in Python — a missing day
      must be a zero, not an absent point, or the sparkline lies about its x-axis:

```python
    def token_series(self, project_id: int, days: int, now: int) -> list[int]:
        """Daily token totals, oldest first, exactly `days` long.

        Gaps are filled with zeros: a sparkline whose x-axis skips idle days
        compresses time and misrepresents the shape.

        Sums `tokens_in + tokens_out` and NOT the cache columns, matching
        `token_totals` exactly. The sparkline renders inches from that method's
        "23k today", so a broader definition here would draw a line whose
        magnitude visibly disagrees with the number beside it. If the definition
        of burn ever changes, both must change together.
        """
        start = now - days * 86400
        with self._lock:
            rows = self.conn.execute(
                "SELECT (ended_epoch - ?) / 86400 AS bucket, "
                "COALESCE(SUM(tokens_in + tokens_out),0) AS total "
                "FROM sessions WHERE project_id=? AND ended_epoch >= ? "
                "GROUP BY bucket", (start, project_id, start)).fetchall()
        series = [0] * days
        for row in rows:
            bucket = int(row["bucket"])
            if 0 <= bucket < days:
                series[bucket] = int(row["total"] or 0)
        return series
```

- [ ] **Step 4: Implement the geometry** as a pure function in `cards.py`:

```python
def spark_points(values: list[int], width: int = 72, height: int = 20) -> str:
    """SVG polyline points for a token-burn sparkline.

    Flat series (including all-zero, the common idle case) render at the
    baseline rather than dividing by a zero range.
    """
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo
    step = width / (len(values) - 1) if len(values) > 1 else 0.0
    out = []
    for i, v in enumerate(values):
        # y is inverted: SVG's origin is top-left, so a peak is y=0.
        y = height - (v - lo) / span * height if span else height
        out.append(f"{i * step:.1f},{y:.1f}")
    return " ".join(out)
```

- [ ] **Step 5: Populate and render.** `spark=store.token_series(row["id"], 7, now)` in
      `build_cards`, and in the burn line:

```jinja
    {% if card.spark %}
      {# aria-hidden: the `23k today / 20k last 5h` text beside it is the
         accessible representation, and a 7-point line adds no detail. #}
      <svg class="spark" viewBox="0 0 72 20" width="72" height="20"
           aria-hidden="true" focusable="false">
        <polyline points="{{ card.spark | spark_points }}" fill="none"
                  stroke="currentColor" stroke-width="1.5"
                  stroke-linejoin="round" stroke-linecap="round"/>
      </svg>
    {% endif %}
```

      Register `spark_points` as a Jinja filter next to the existing `kilo`.

- [ ] **Step 6: Run, write** `tools/mutations/phase4-task6.json`**, run it, commit.**

```bash
git commit -m "Draw a seven-day token sparkline on each card"
```

**Tests (falsification required):**
- [ ] All-zero is flat at the baseline. *Mutation: drop the `if span else height` guard →
      `ZeroDivisionError` on every idle card, which is most of them.*
- [ ] Missing days are zeros. *Mutation: return only the rows found → a project active on two days
      renders a 2-point line labelled as a week.*
- [ ] Oldest-first ordering. *Mutation: reverse the series → the trend reads backwards, so a project
      winding down looks like one ramping up.*
- [ ] y is inverted. *Mutation: `y = (v - lo) / span * height` → every sparkline is upside down.*
- [ ] The series matches `token_totals`'s definition. *Mutation: add the cache columns to the SUM →
      the line's magnitude silently contradicts the burn text beside it.*

---

### Task 7: Diagnostics

**Files:** modify `src/bridge/store.py`, `src/bridge/indexer.py`, `src/bridge/api.py`,
`src/bridge/templates/base.html`; create `src/bridge/templates/diagnostics.html`; test
`tests/test_store.py`, `tests/test_indexer.py`, `tests/test_api.py`

**Interfaces:**
```python
# New table, appended to SCHEMA (additive; never a rebuild)
index_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, ran_at INTEGER NOT NULL,
           files_seen INTEGER, files_scanned INTEGER, lines_parsed INTEGER,
           parse_errors INTEGER, sessions_upserted INTEGER, duration_ms INTEGER)

Store.record_index_run(stats: dict, ran_at: int, duration_ms: int) -> None
Store.latest_index_run() -> sqlite3.Row | None
GET /api/diagnostics -> {"last_index": {...}, "parse_errors": int, "spool_depth": int,
                         "probe_failures": int, "live": "ok"|"unavailable",
                         "running_sessions": int, "queued_handoffs": int}
GET /diagnostics      -> HTML
```

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
def test_an_index_run_is_recorded_so_diagnostics_has_something_to_read(store, tmp_path):
    indexer.index(store, cfg)
    row = store.latest_index_run()
    assert row is not None and row["files_seen"] > 0


def test_diagnostics_reports_parse_errors_from_the_last_run(client, store):
    store.record_index_run({"parse_errors": 3, "files_seen": 9}, ran_at=100, duration_ms=5)
    body = client.get("/api/diagnostics").json()
    assert body["parse_errors"] == 3


def test_diagnostics_counts_undrained_spool_files(client, cfg):
    (cfg.spool_dir / "x.json").write_text("{}")
    assert client.get("/api/diagnostics").json()["spool_depth"] == 1


def test_drained_spool_files_are_not_counted_as_depth(client, cfg):
    """`spool/drained/` is history, not backlog."""
    (cfg.spool_dir / "drained").mkdir(parents=True, exist_ok=True)
    (cfg.spool_dir / "drained" / "x.json").write_text("{}")
    assert client.get("/api/diagnostics").json()["spool_depth"] == 0


def test_the_header_links_to_diagnostics_only_when_something_is_wrong(client, store):
    store.record_index_run({"parse_errors": 0}, ran_at=1, duration_ms=1)
    assert "data-diagnostics-alert" not in client.get("/").text
    store.record_index_run({"parse_errors": 2}, ran_at=2, duration_ms=1)
    assert "data-diagnostics-alert" in client.get("/").text


def test_diagnostics_survives_never_having_indexed(client):
    """A fresh install has no runs; the route must answer, not 500."""
    assert client.get("/api/diagnostics").status_code == 200
```

- [ ] **Step 2: Run and watch fail.** `404` on `/api/diagnostics`.

- [ ] **Step 3: Add the table and the store pair.** Append the `CREATE TABLE IF NOT EXISTS` to
      `SCHEMA`. `record_index_run` takes the stats dict `indexer.index` already returns, so the
      indexer's return shape stays the contract and nothing new is invented.

- [ ] **Step 4: Record from the indexer.** At the end of `index()`, time the run and call
      `record_index_run`. It must not be able to fail the scan: wrap it so a diagnostics write
      cannot lose an index. Keep returning the same dict — `bridge index` prints it and
      `tests/test_indexer.py` asserts on it.

- [ ] **Step 5: Add both routes.** `/api/diagnostics` assembles the numbers; `probe_failures` and
      `live` come from building cards, which is already what `/` does. `/diagnostics` renders them in
      a plain table. In `base.html`, show the affordance only when `parse_errors > 0` or the spool is
      non-empty — a permanent "diagnostics" link would train the eye to ignore it.

- [ ] **Step 6: Run, write** `tools/mutations/phase4-task7.json`**, run it, commit.**

```bash
git commit -m "Record index runs and expose a diagnostics view"
```

**Tests (falsification required):**
- [ ] Drained files are excluded from depth. *Mutation: count the directory recursively → depth grows
      forever and permanently claims a backlog that was drained.*
- [ ] The header affordance is conditional. *Mutation: always show it → the one signal that something
      is wrong becomes furniture.*
- [ ] A failed diagnostics write cannot fail an index. *Mutation: remove the guard → a diagnostics bug
      takes out indexing, the one thing that must always work.*
- [ ] The route answers before any index has run. *Mutation: index `[0]` of the runs → 500 on a fresh
      install.*

---

### Task 8: SSE

**Files:** modify `src/bridge/api.py`, `src/bridge/templates/base.html`; create
`src/bridge/static/live.js`; test `tests/test_api.py`, `tests/test_static_js.py`

**Interfaces:**
```
GET /events   ->   text/event-stream
                   data: {"live": {"<project_path>": {"status": "busy", "started_at": 1785548714}},
                          "index": {"ran_at": ..., "parse_errors": 0}}\n\n
                   : heartbeat\n\n      (comment frame, keeps the connection open)
```

**Steps:**

- [ ] **Step 1: Write the failing tests.** The two risks are a frozen panel and a destroyed edit, so
      test those rather than the happy path. Timing is injected — no test sleeps:

```python
def test_events_streams_a_tick_in_sse_frame_format(client):
    with client.stream("GET", "/events?max_ticks=1&interval=0") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    assert body.startswith("data: ")
    assert body.endswith("\n\n")
    json.loads(body[len("data: "):].strip())   # parses


def test_an_open_stream_does_not_block_other_requests(client):
    """The store is ONE connection behind ONE lock. A stream that holds it
    across its sleep freezes the entire panel.
    """
    with client.stream("GET", "/events?max_ticks=3&interval=0.05"):
        start = time.monotonic()
        assert client.get("/api/projects").status_code == 200
        assert time.monotonic() - start < 0.05


def test_a_tick_carries_live_state_keyed_by_project_path(client, store):
    ...


def test_live_js_never_touches_the_prompt_textarea():
    """The handoff prompt is the only state Bridge cannot rebuild.

    Executed with node resolved by ABSOLUTE PATH: `tools/falsify.py` runs
    pytest with PATH=/usr/bin:/bin, and a bare `node` would make this SKIP,
    reporting SURVIVED for a mutation this test would actually catch.
    """
    source = (STATIC / "live.js").read_text()
    assert "data-prompt-handoff" not in source
    assert ".innerHTML" not in source        # no subtree replacement
    assert "location.reload" not in source
```

- [ ] **Step 2: Run and watch fail.** `404` on `/events`.

- [ ] **Step 3: Implement the endpoint** with `StreamingResponse` and a plain generator. No new
      dependency: `sse-starlette` would add one for framing that is three lines of string
      formatting, and `pyproject.toml` gaining a dependency means everyone re-runs
      `uv tool install --editable --force`.

```python
    @app.get("/events")
    def events(max_ticks: int | None = None, interval: float = 3.0):
        """Push live deltas as SSE. Bounded by `max_ticks` so tests terminate.

        The store is a single connection behind a single lock, so each poll
        acquires it, copies what it needs, and releases it BEFORE sleeping. A
        generator that sleeps while holding the lock freezes every other route.
        """
        def stream():
            ticks = 0
            while max_ticks is None or ticks < max_ticks:
                payload = _live_payload(store, cfg)   # takes and releases the lock
                yield f"data: {json.dumps(payload)}\n\n"
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                time.sleep(interval)                  # lock NOT held here
                yield ": heartbeat\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
```

- [ ] **Step 4: Write `live.js`** to patch only the live band, the burn text, and the sparkline. It
      must not touch the handoff textarea, must not `innerHTML` a card, and must not reload:

```js
// Live updates, patched into specific nodes.
//
// This file deliberately cannot see the handoff <textarea>. A reload or an
// innerHTML swap over a card would discard an in-progress prompt edit, which
// is the one piece of state Bridge cannot rebuild from transcripts. So: text
// content on leaf nodes, nothing structural.
const source = new EventSource("/events");
source.addEventListener("message", (event) => {
  let payload;
  try {
    payload = JSON.parse(event.data);
  } catch (error) {
    console.error("bridge: malformed live payload", error);
    return;   // a bad frame is skipped; EventSource keeps the stream
  }
  for (const [path, live] of Object.entries(payload.live || {})) {
    const band = document.querySelector(`[data-live-path="${CSS.escape(path)}"]`);
    if (band) band.textContent = live.status === "busy" ? "running" : "idle";
  }
});
```

      `EventSource` reconnects on its own, so there is no retry logic to write. Add
      `data-live-path="{{ card.path }}"` to the band from Task 5, and the `<script>` to `base.html`.

- [ ] **Step 5: Confirm the sole-writer architecture is intact.** `/events` reads; it must not index,
      launch, or write. Add an assertion that a tick performs no writes if the suite has an idiom for
      it; otherwise state it in the docstring and keep the generator's calls read-only.

- [ ] **Step 6: Run, write** `tools/mutations/phase4-task8.json`**, run it, commit.**

```bash
git commit -m "Push live updates over SSE"
```

**Tests (falsification required):**
- [ ] An open stream does not block other requests. *Mutation: hold the store lock across the sleep →
      the whole panel hangs while a tab is open. The single worst regression available in this phase.*
- [ ] Frames end with a blank line. *Mutation: single `\n` → the browser buffers forever and no event
      ever fires, with no error anywhere.*
- [ ] A malformed frame is skipped, not fatal. *Mutation: drop the try/catch → one bad payload kills
      live updates for the session.*
- [ ] `live.js` never references the prompt field or `innerHTML`. *Mutation: patch the card by
      `innerHTML` → a live tick silently discards whatever the user was typing.*

---

## Out of scope, and why

Carried forward from the Phase 3 handoff and not addressed here:

- **`~/.bridge/config.toml`.** Decision 3. The model catalog changes shape in Task 1; changing where
  it *lives* at the same time would conflate two things.
- **Incremental rescan at ~0.22s against the spec's 200ms.** 10% over a round number, on a path that
  is not user-visible. SSE makes scan *frequency* matter, so revisit if a tick ever feels slow —
  Task 7's `duration_ms` is recorded precisely so that becomes a measurement rather than a guess.
- **The window meter.** Decision 7: no published denominator for the 5h window, so the bar would be
  decorative.
- **Killing or signalling a session from the panel.** `agents` gives us `pid`, which makes this look
  trivial. It violates the standing constraint that Bridge launches but never supervises, and it
  belongs to a phase that can think about confirmation semantics.
- **The `spool.journal_status` second-granularity collision** documented in the Phase 3 handoff.
  Unchanged and still deliberate: every colliding value is terminal and non-queued, so a launched
  prompt cannot replay as queued.

---

## Self-Review

**1. Spec coverage.** §3 `agents` → Task 3. §4 `git_cache` → Task 4. §6 `GET /events` → Task 8,
`GET /api/diagnostics` → Task 7. Card layout (lines 336–370): running band → Task 5, sparkline →
Task 6, order → Task 5. Degradation table (405–409): `agents` failure → Task 3/5, git timeout →
Task 4, malformed JSONL → Task 7. Phase 4 bullet (451): SSE, agents probe, sparklines, diagnostics
all covered; **window meter deliberately deferred** with a reason. Mit's two requests → Tasks 1 and 2.

Gap accepted: the spec's `PATCH /api/projects/{id}` (pin/archive/hide) is still unimplemented from
Phase 1 and is not in this plan. It is unrelated to liveness and does not block anything here.

**2. Placeholder scan.** No TBDs. Every code step carries real code; every model id was extracted
from the installed binary; the `agents` fixture is the literal recorded payload. One step
deliberately defers to the codebase rather than inventing: the contrast-test iteration in Task 2
Step 6 names the file to follow, which is a direction, not a placeholder.

**3. Type consistency.** `LiveSession` and `AgentsState` are defined in Task 3's interface block and
used with the same field names in Tasks 5 and 8. `ModelChoice(value, label)` is consistent across
Task 1's config, `model_options`, and the template. `GitState.cached_at` is written in Task 4 and read
in Task 4's template only. `Store.token_series(project_id, days, now)` matches its Task 6 call site.
`spark_points` is the same name as the function, the Jinja filter, and the mutation targets.
`RANK_*` renumbering is applied in one place and the ordering test asserts the relative result, not
the literals — so the constants can move again without rewriting the test.

Three inconsistencies were found and fixed while reviewing, each by checking the codebase rather than
trusting the draft:

1. Task 5's live band read `card.live.model`, which does not exist — `claude agents --json` has no
   `model` field. It now reads `card.session.model`, with a comment saying why so the next person
   does not "fix" it back.
2. Tasks 4 and 5 piped epoch integers through the `ago` filter. `api.py:119-120` registers **two**
   filters — `ago` for ISO-8601 strings, `ago_epoch` for epoch ints — and `cached_at` and
   `started_at` are both ints. Corrected to `ago_epoch`, and both now supply the trailing "ago" the
   way every existing call site does.
3. Task 6's `token_series` summed all four token columns while `token_totals` (`store.py:464`) sums
   only `tokens_in + tokens_out`. The sparkline renders beside that method's output, so the two
   definitions must agree; the query was narrowed and a test now pins the two together, because
   nothing else would ever catch the drift.
