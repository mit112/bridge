# Multiple Concurrent Handoffs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let several handoffs stay queued for one project at once, each shown as its own fireable item, so two sessions on the same project both leave a handoff and the user chooses which to fire, when.

**Architecture:** One behavioral change carries the feature — `create_handoff` supersedes only handoffs from the *same authoring session* (`source_session_id`) instead of every queued handoff in the project. Everything else is exposing the now-plural set through the read path: store → cards → api → overview/projects → templates. A backward-compat `Card.handoff` property (newest of the list) lets each layer migrate independently without a flag day.

**Tech Stack:** Python 3.13, FastAPI, SQLite (`sqlite3`), Jinja2 templates, vanilla JS. Tests: `uv run pytest` (NOT bare `pytest`).

## Global Constraints

- Test runner is **`uv run pytest`**, never bare `pytest`. Suite is ~1025 passing at plan start; keep it green.
- **No schema migration.** `handoffs.source_session_id` already exists and is populated. This feature changes SQL predicates and read shapes only.
- **Never pipe pytest to `head`/`tail` before a commit** — it masks the exit code (this is zsh, no `PIPESTATUS`). Run pytest as its own command, read the result, then commit.
- Surgical diffs: every changed line traces to this feature. Do not reformat adjacent code.
- The single-queued invariant is preserved *per session*: at most one queued handoff per `(project_id, source_session_id)`; unbounded such pairs per project. When `source_session_id IS NULL`, never supersede (each anonymous handoff stands alone).

---

### Task 1: Store — session-scoped supersede + plural reader

**Files:**
- Modify: `src/bridge/store.py:400-437` (`create_handoff`, `queued_handoff`)
- Test: `tests/test_store.py` (add alongside existing handoff store tests)

**Interfaces:**
- Produces: `Store.queued_handoffs(project_id: int) -> list[sqlite3.Row]` — all `status='queued'` rows for the project, `ORDER BY created_at DESC`.
- Produces: `Store.create_handoff(h: Handoff, project_id: int) -> str` — unchanged signature; supersede now scoped by `source_session_id`.
- Keep `Store.queued_handoff(project_id) -> sqlite3.Row | None` returning the newest queued (first of `queued_handoffs`), so existing callers keep compiling while later tasks migrate them.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`. (Use the file's existing `Handoff`/`Store` construction helpers; if it builds handoffs inline, mirror that. `source_session_id` is the field that scopes supersession.)

```python
def test_two_sessions_coexist(store):
    p = store.upsert_project("/proj/a")
    store.create_handoff(Handoff(id="h1", project_path="/proj/a",
        next_prompt="plan", source_session_id="sess-1"), p)
    store.create_handoff(Handoff(id="h2", project_path="/proj/a",
        next_prompt="ui", source_session_id="sess-2"), p)
    ids = {r["id"] for r in store.queued_handoffs(p)}
    assert ids == {"h1", "h2"}


def test_same_session_supersedes(store):
    p = store.upsert_project("/proj/a")
    store.create_handoff(Handoff(id="h1", project_path="/proj/a",
        next_prompt="v1", source_session_id="sess-1"), p)
    store.create_handoff(Handoff(id="h2", project_path="/proj/a",
        next_prompt="v2", source_session_id="sess-1"), p)
    rows = store.queued_handoffs(p)
    assert [r["id"] for r in rows] == ["h2"]
    assert store.get_handoff("h1")["status"] == "superseded"


def test_null_session_never_supersedes(store):
    p = store.upsert_project("/proj/a")
    store.create_handoff(Handoff(id="h1", project_path="/proj/a",
        next_prompt="a", source_session_id=None), p)
    store.create_handoff(Handoff(id="h2", project_path="/proj/a",
        next_prompt="b", source_session_id=None), p)
    ids = {r["id"] for r in store.queued_handoffs(p)}
    assert ids == {"h1", "h2"}


def test_redrain_is_idempotent_and_leaves_siblings(store):
    # Re-inserting an already-queued handoff (spool re-drain) must not supersede
    # a sibling from another session.
    p = store.upsert_project("/proj/a")
    h = Handoff(id="h1", project_path="/proj/a", next_prompt="plan",
                source_session_id="sess-1")
    store.create_handoff(h, p)
    store.create_handoff(Handoff(id="h2", project_path="/proj/a",
        next_prompt="ui", source_session_id="sess-2"), p)
    store.create_handoff(h, p)  # re-drain of h1
    ids = {r["id"] for r in store.queued_handoffs(p)}
    assert ids == {"h1", "h2"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_store.py -k "coexist or supersede or null_session or redrain" -v`
Expected: FAIL — `queued_handoffs` does not exist; coexistence fails because the current supersede is project-wide.

- [ ] **Step 3: Implement the scoped supersede and plural reader**

In `create_handoff`, replace the supersede statement so it is scoped by session and skipped for a null session:

```python
def create_handoff(self, h: Handoff, project_id: int) -> str:
    """Queue a handoff, superseding any already queued for the SAME session.

    Supersession is scoped to `source_session_id` so two different sessions on
    one project each keep their queued handoff, while re-running the skill in the
    same session still replaces that session's own prompt. A null session never
    supersedes: anonymous handoffs have no identity to collapse against, so each
    stands alone. The `id<>?` guard and `ON CONFLICT(id) DO NOTHING` keep a spool
    re-drain idempotent exactly as before.
    """
    with self.transaction():
        if h.source_session_id is not None:
            self.conn.execute(
                "UPDATE handoffs SET status='superseded' "
                "WHERE project_id=? AND status='queued' "
                "AND source_session_id=? AND id<>?",
                (project_id, h.source_session_id, h.id),
            )
        self.conn.execute(
            "INSERT INTO handoffs(id, project_id, source_session_id, summary, "
            "next_prompt, suggested_model, suggested_effort, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (
                h.id, project_id, h.source_session_id, h.summary, h.next_prompt,
                h.suggested_model, h.suggested_effort, h.status or "queued",
                h.created_at or now_epoch(),
            ),
        )
    return h.id
```

Add `queued_handoffs` and keep `queued_handoff` as the newest-of:

```python
def queued_handoffs(self, project_id: int) -> list[sqlite3.Row]:
    with self._lock:
        return list(self.conn.execute(
            "SELECT * FROM handoffs WHERE project_id=? AND status='queued' "
            "ORDER BY created_at DESC",
            (project_id,),
        ))

def queued_handoff(self, project_id: int) -> sqlite3.Row | None:
    """The newest queued handoff, or None. Retained for callers that still want
    a single 'what's next'; `queued_handoffs` is the full set."""
    rows = self.queued_handoffs(project_id)
    return rows[0] if rows else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_store.py -k "coexist or supersede or null_session or redrain" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the full store suite for regressions**

Run: `uv run pytest tests/test_store.py -q`
Expected: PASS — the existing single-handoff tests still pass, because a lone session's handoff behaves identically.

- [ ] **Step 6: Commit**

```bash
git add src/bridge/store.py tests/test_store.py
git commit -m "Scope handoff supersession to the authoring session"
```

---

### Task 2: Cards — expose all queued handoffs with a compat property

**Files:**
- Modify: `src/bridge/models.py:176-196` (`Card` dataclass — `handoff` field)
- Modify: `src/bridge/cards.py:439-441` (`_handoff` helper), and the `build_cards` call site that sets `handoff=`
- Test: `tests/test_cards.py`

**Interfaces:**
- Consumes: `Store.queued_handoffs` from Task 1.
- Produces: `Card.handoffs: list[dict]` — every queued handoff for the project as plain dicts, newest first (empty list when none).
- Produces: `Card.handoff` — a read-only `@property` returning `self.handoffs[0] if self.handoffs else None`. Existing readers (`sort_key`, templates, overview) keep working unchanged against the newest handoff until later tasks migrate them.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cards.py`:

```python
def test_card_carries_all_queued_handoffs(store, cfg):
    p = store.upsert_project("/proj/a")
    store.create_handoff(Handoff(id="h1", project_path="/proj/a",
        next_prompt="plan", source_session_id="sess-1"), p)
    store.create_handoff(Handoff(id="h2", project_path="/proj/a",
        next_prompt="ui", source_session_id="sess-2"), p)
    cards = build_cards(store, cfg)
    card = next(c for c in cards if c.path == "/proj/a")
    assert {h["id"] for h in card.handoffs} == {"h1", "h2"}
    # Compat property returns the newest (h2 is created last).
    assert card.handoff["id"] == "h2"


def test_card_no_handoffs_is_empty_list(store, cfg):
    p = store.upsert_project("/proj/b")
    cards = build_cards(store, cfg)
    card = next(c for c in cards if c.path == "/proj/b")
    assert card.handoffs == []
    assert card.handoff is None
```

(If `build_cards`/`cfg` fixtures differ in `tests/test_cards.py`, mirror the file's existing card-construction pattern; the assertions on `card.handoffs` / `card.handoff` are the contract.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cards.py -k "all_queued or no_handoffs_is_empty" -v`
Expected: FAIL — `Card` has no `handoffs` attribute.

- [ ] **Step 3: Change the `Card` field to a list with a compat property**

In `models.py`, replace the `handoff` field (currently `handoff: dict | None = None`, ~line 196) with a list field plus a property. Update the comment that asserts "at most one":

```python
    # Every queued handoff for this project, newest first. A project can carry
    # several at once -- one per authoring session -- and the launch surface
    # renders one fireable block per entry. Empty when nothing is queued.
    handoffs: list[dict] = field(default_factory=list)

    @property
    def handoff(self) -> dict | None:
        """The newest queued handoff, or None. Compatibility shim for readers
        that still think in terms of a single 'what's next'; new code iterates
        `handoffs`."""
        return self.handoffs[0] if self.handoffs else None
```

Ensure `field` is imported (it already is — `models.py` uses `field(default_factory=list)` for `spark`).

- [ ] **Step 4: Populate the list in `cards.py`**

Replace `_handoff` and its call site. Rename to `_handoffs`:

```python
def _handoffs(store: Store, project_id: int) -> list[dict]:
    return [dict(row) for row in store.queued_handoffs(project_id)]
```

At the `build_cards` construction site, change `handoff=_handoff(store, project_id)` to `handoffs=_handoffs(store, project_id)`. (Grep `_handoff(` to find the single call site.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cards.py -k "all_queued or no_handoffs_is_empty" -v`
Expected: PASS.

- [ ] **Step 6: Run the card + sort suites for regressions**

Run: `uv run pytest tests/test_cards.py -q`
Expected: PASS — `sort_key` reads `card.handoff` (the property), still truthy when anything is queued, so ranking is unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/bridge/models.py src/bridge/cards.py tests/test_cards.py
git commit -m "Carry all queued handoffs on the card with a newest-of compat property"
```

---

### Task 3: API — plural read endpoints and deterministic launch fallback

**Files:**
- Modify: `src/bridge/api.py:960-983` (`GET /api/handoff`, `GET /api/handoff/{project_id}`)
- Modify: `src/bridge/api.py:1042-1057` (launch fallback in `post_launch`)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `Store.queued_handoffs`, `Store.queued_handoff` from Task 1.
- Produces: `GET /api/handoffs?project_path=` → `200` JSON list of queued handoffs (empty list, not 204, when none); the project-scoped `GET /api/handoff/{project_id}` likewise returns a list.
- Produces: `POST /api/launch` — when `prompt` is omitted **and** `handoff_id` is omitted, no longer silently grabs an arbitrary queued handoff; it 422s asking for an explicit handoff. An explicit `handoff_id` still fires exactly that one.

**Decision (resolves the spec's "list vs plural sibling"):** keep the existing single-lookup routes working for `bridge next` (they return the newest via `queued_handoff`), and ADD a plural route the panel uses. This avoids breaking the CLI's `bridge next` contract while giving the UI the full set.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py` (use the file's existing `client` fixture and handoff-POST helper):

```python
def test_handoffs_plural_returns_all(client):
    client.post("/api/handoff", json={"id": "h1", "project_path": "/proj/a",
        "next_prompt": "plan", "session_id": "s1"})
    client.post("/api/handoff", json={"id": "h2", "project_path": "/proj/a",
        "next_prompt": "ui", "session_id": "s2"})
    r = client.get("/api/handoffs", params={"project_path": "/proj/a"})
    assert r.status_code == 200
    assert {h["id"] for h in r.json()} == {"h1", "h2"}


def test_handoffs_plural_empty_is_empty_list(client):
    r = client.get("/api/handoffs", params={"project_path": "/proj/none"})
    assert r.status_code == 200
    assert r.json() == []


def test_launch_without_prompt_or_handoff_is_422(client):
    client.post("/api/handoff", json={"id": "h1", "project_path": "/proj/a",
        "next_prompt": "plan", "session_id": "s1"})
    r = client.post("/api/launch", json={"project_path": "/proj/a"})
    assert r.status_code == 422


def test_launch_with_explicit_handoff_fires_that_one(client):
    client.post("/api/handoff", json={"id": "h1", "project_path": "/proj/a",
        "next_prompt": "plan", "session_id": "s1"})
    client.post("/api/handoff", json={"id": "h2", "project_path": "/proj/a",
        "next_prompt": "ui", "session_id": "s2"})
    r = client.post("/api/launch", json={"project_path": "/proj/a",
        "handoff_id": "h1"})
    assert r.status_code == 200
    # h1 consumed, h2 still queued.
    remaining = client.get("/api/handoffs", params={"project_path": "/proj/a"}).json()
    assert {h["id"] for h in remaining} == {"h2"}
```

(If `tests/test_api.py` stubs the actual spawn, mirror that stub so `POST /api/launch` returns 200 without launching a real process. Grep the file for how existing launch tests neutralise `fire`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api.py -k "handoffs_plural or launch_without_prompt or explicit_handoff_fires" -v`
Expected: FAIL — `/api/handoffs` is 404 (route absent); the launch-fallback test still auto-grabs h-newest and returns 200 instead of 422.

- [ ] **Step 3: Add the plural routes**

After `get_handoff` (~line 983) add:

```python
    @app.get("/api/handoffs")
    def list_handoffs_by_path(project_path: str):
        """Every queued handoff for a project, for the panel's stacked view.
        Returns [] (not 204) for an unknown or empty project, so the client
        renders 'nothing queued' without special-casing a no-content status."""
        canonical = store.alias_map().get(project_path, project_path)
        project = store.project_by_path(canonical)
        if project is None:
            return []
        return [dict(r) for r in store.queued_handoffs(project["id"])]

    @app.get("/api/handoffs/{project_id}")
    def list_handoffs(project_id: int):
        return [dict(r) for r in store.queued_handoffs(project_id)]
```

Leave the singular `GET /api/handoff` routes as-is (they now return the newest via `queued_handoff`, which is the correct "what's next" for `bridge next`).

- [ ] **Step 4: Make the launch fallback deterministic**

In `post_launch`, drop the implicit "grab the project's queued handoff" branch. Replace lines ~1046-1057:

```python
        prompt, handoff_id = body.prompt, body.handoff_id
        if prompt is None and handoff_id is None:
            raise HTTPException(
                status_code=422,
                detail="supply a prompt or a handoff_id; a project may have "
                       "several queued handoffs and the target must be explicit",
            )
        if prompt is None:
            handoff = store.get_handoff(handoff_id)
            if handoff is None:
                raise HTTPException(status_code=404, detail="unknown handoff")
            prompt = handoff["next_prompt"]
```

Remove the now-unused `canonical = ...` / `project = ...` / `queued = ...` block that fed the old fallback IF nothing else below uses `canonical`. (Grep the rest of `post_launch` for `canonical` first — if the title/default path still references it, keep that computation and only remove the `queued = store.queued_handoff(...)` line and the `if queued is None` arm.)

The existing `handoff = store.get_handoff(handoff_id) if handoff_id else None` / 404 / title block below still applies when an explicit `handoff_id` came in with a prompt; keep it. Consolidate so `get_handoff` is not called twice for the same id (fetch once, reuse for both prompt and title).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api.py -k "handoffs_plural or launch_without_prompt or explicit_handoff_fires" -v`
Expected: PASS.

- [ ] **Step 6: Run the full api suite for regressions**

Run: `uv run pytest tests/test_api.py -q`
Expected: PASS. If a pre-existing test relied on the implicit no-arg launch fallback, update it to pass an explicit `handoff_id` — that is the intended contract change, and the test comment should say so.

- [ ] **Step 7: Commit**

```bash
git add src/bridge/api.py tests/test_api.py
git commit -m "Add plural handoff read routes and require an explicit launch target"
```

---

### Task 4: Overview & projects — one attention item per handoff

**Files:**
- Modify: `src/bridge/overview.py:213-237` (attention loop), `overview.py:358` (`_status_word` — no change needed, uses property)
- Modify: `src/bridge/projects_view.py:82,85` (counts)
- Test: `tests/test_overview.py`, `tests/test_projects_view.py`

**Interfaces:**
- Consumes: `Card.handoffs` from Task 2.
- Produces: attention feed emits one `AttentionItem(kind="handoff", ...)` per queued handoff, each carrying its own `handoff_id`/`summary`/`created_at` in `meta`, so each is independently actionable.
- Produces: projects "queued" count reflects the number of queued handoffs (was: number of cards with a handoff).

- [ ] **Step 1: Write the failing tests**

In `tests/test_overview.py`:

```python
def test_attention_emits_one_item_per_handoff(store, cfg):
    p = store.upsert_project("/proj/a")
    store.create_handoff(Handoff(id="h1", project_path="/proj/a",
        next_prompt="plan", summary="Planned", source_session_id="s1"), p)
    store.create_handoff(Handoff(id="h2", project_path="/proj/a",
        next_prompt="ui", summary="UI work", source_session_id="s2"), p)
    cards = build_cards(store, cfg)
    items = attention_items(cards)   # use the real symbol name in overview.py
    handoff_items = [i for i in items if i.kind == "handoff"]
    assert {i.meta["handoff_id"] for i in handoff_items} == {"h1", "h2"}
```

In `tests/test_projects_view.py`:

```python
def test_queued_count_counts_handoffs_not_cards(store, cfg):
    p = store.upsert_project("/proj/a")
    store.create_handoff(Handoff(id="h1", project_path="/proj/a",
        next_prompt="plan", source_session_id="s1"), p)
    store.create_handoff(Handoff(id="h2", project_path="/proj/a",
        next_prompt="ui", source_session_id="s2"), p)
    summary = project_summary(...)  # mirror the file's existing call shape
    assert summary.queued == 2
```

(Match the real function names — grep `overview.py` for the attention-building entrypoint and `projects_view.py` for `queued`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_overview.py tests/test_projects_view.py -k "one_item_per_handoff or counts_handoffs" -v`
Expected: FAIL — one item emitted, count is 1 (cards-with-handoff), not 2.

- [ ] **Step 3: Emit one attention item per handoff**

In `overview.py`, change the `if card.handoff:` branch (~line 215) to loop `card.handoffs`:

```python
        for h in card.handoffs:
            items.append(AttentionItem(
                kind="handoff",
                project_id=card.project_id,
                title=(card.session.title if card.session and card.session.title
                       else card.name),
                summary=(h.get("summary") or h.get("next_prompt", "")),
                primary_action=Action(
                    "Continue in Terminal", f"/project/{card.project_id}?tab=current",
                ),
                meta={
                    "handoff_id": h.get("id"),
                    "project_name": card.name,
                    "created_at": h.get("created_at"),
                    "has_span": bool(card.session),
                    "branch": card.git.branch,
                    "dirty_count": card.git.dirty_count,
                    "path": card.path,
                },
            ))
        if not card.handoffs and card.live is not None:
            ...  # existing `elif card.live` body, now guarded by `if not card.handoffs`
```

Convert the `elif card.live` / `elif card.is_stale` chain that followed `if card.handoff:` into `if not card.handoffs:` guarded branches (a card with handoffs should still not also emit a running/stale item, matching today's `elif` behaviour). Keep `_status_word` unchanged — it reads `card.handoff` (the property) and "queued" if any handoff exists is still correct.

- [ ] **Step 4: Count handoffs in projects_view**

In `projects_view.py`, the `queued` count (~line 85) becomes the sum of handoffs:

```python
        "queued": sum(len(card.handoffs) for card in cards),
```

Line 82's attention-worthiness check (`card.handoff or card.live is not None or card.is_stale`) still works via the property; leave it, or make it explicit with `card.handoffs`. Prefer `card.handoffs` for clarity:

```python
            1 for card in cards if card.handoffs or card.live is not None or card.is_stale
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_overview.py tests/test_projects_view.py -k "one_item_per_handoff or counts_handoffs" -v`
Expected: PASS.

- [ ] **Step 6: Run both suites for regressions**

Run: `uv run pytest tests/test_overview.py tests/test_projects_view.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/bridge/overview.py src/bridge/projects_view.py tests/test_overview.py tests/test_projects_view.py
git commit -m "Surface every queued handoff in the attention feed and counts"
```

---

### Task 5: Templates — stack a fireable block per handoff

**Files:**
- Modify: `src/bridge/templates/_launch.html:85-194` (`handoff_block`, `handoff_actions`, `launch_band` — parameterize on an explicit handoff)
- Modify: `src/bridge/templates/_workspace_current.html:14-44` (loop the blocks)
- Modify: the dashboard card template that renders the per-card handoff (grep `handoff_block(` / `card.handoff` under `templates/` for the dashboard/cards partial)
- Test: `tests/test_shell_contract.py`, and any template-render test (grep `handoff_block` in `tests/`)

**Interfaces:**
- Consumes: `Card.handoffs` from Task 2.
- Produces: macros `handoff_block(card, handoff, totals, ...)`, `handoff_actions(card, handoff, totals, ...)`, `launch_band(card, handoff, ...)` that take an explicit `handoff` dict instead of reading `card.handoff`. Callers loop `for h in card.handoffs`.

**Why parameterize:** every macro currently reads `card.handoff` and derives `hid = "handoff-" ~ card.handoff.id`. To render N blocks, each needs *its own* handoff; passing it explicitly is the minimal change and keeps the per-handoff `data-*` id hooks (which JS already keys on) correct per block.

- [ ] **Step 1: Write/extend the failing contract test**

Add to the template-render test (mirror how existing tests render a card to HTML — grep `tests/` for `handoff_block` or a Jinja `Environment` fixture). Assert two blocks render for two handoffs:

```python
def test_two_handoffs_render_two_blocks(render_card):
    card = make_card(handoffs=[
        {"id": "h1", "next_prompt": "plan", "summary": "Planned",
         "created_at": 1, "suggested_model": None, "suggested_effort": None},
        {"id": "h2", "next_prompt": "ui", "summary": "UI work",
         "created_at": 2, "suggested_model": None, "suggested_effort": None},
    ])
    html = render_card(card)
    assert 'data-handoff-section="h1"' in html
    assert 'data-handoff-section="h2"' in html
    assert 'data-launch-handoff="h1"' in html
    assert 'data-launch-handoff="h2"' in html
```

If `tests/test_shell_contract.py` asserts a fixed count of handoff sections, extend it to allow N.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_shell_contract.py -k "two_handoffs or handoff" -v`
Expected: FAIL — only one block (the newest, via the property) renders.

- [ ] **Step 3: Parameterize the macros**

In `_launch.html`, change the three macro signatures and bodies to take `handoff` and derive from it, e.g.:

```jinja
{% macro handoff_block(card, handoff, totals, collapse_prompt=False, show_span_line=False) %}
  {% if handoff %}
    {% set hid = "handoff-" ~ handoff.id %}
    <section class="handoff" aria-labelledby="{{ hid }}-title"
             data-handoff-section="{{ handoff.id }}">
      ...
      <p class="handoff__kicker">Queued handoff · {{ handoff.created_at | ago_epoch }} ago</p>
      <p class="handoff__summary" ...>{{ handoff.summary or "Continue from the exact saved prompt." }}</p>
      ...
      <textarea class="handoff__prompt" id="{{ hid }}"
                data-prompt-handoff="{{ handoff.id }}">{{ handoff.next_prompt }}</textarea>
```

Do the same for `handoff_actions(card, handoff, totals, show_dismiss=False)` (all `card.handoff.id` → `handoff.id`, `card.handoff.*` → `handoff.*`) and `launch_band(card, handoff, primary=False, collapse_options=False)` (the `sm`/`se` suggested-model/effort reads and `data-launch-handoff`/`data-launch-prompt` become `handoff.id`; the disabled/label ternaries key on `handoff`).

- [ ] **Step 4: Loop the blocks in the workspace Current view**

In `_workspace_current.html`, replace the single `handoff_block(...)` / `launch_band(...)` / `handoff_actions(...)` calls with a loop over `card.handoffs`, and drive the empty-state and compose collapse off `card.handoffs`:

```jinja
<p class="empty" data-handoff-empty="{{ card.project_id }}" {% if card.handoffs %}hidden{% endif %}>
  ...
</p>
{% for h in card.handoffs %}
  {{ handoff_block(card, h, totals, collapse_prompt=True, show_span_line=(loop.first)) }}
  <div class="handoff__launch-row">
    {{ launch_band(card, h, primary=loop.first, collapse_options=True) }}
    {{ handoff_actions(card, h, totals, show_dismiss=True) }}
  </div>
{% endfor %}
{# One always-available compose box for a brand-new prompt, unchanged. #}
{{ compose_box(card, totals, collapsed=(card.handoffs | length > 0)) }}
{# When there are no handoffs, still offer a plain "Start session" launch band. #}
{% if not card.handoffs %}
  {{ launch_band(card, none, primary=True) }}
{% endif %}
```

`launch_band(card, none, ...)` must render the "Start session" (disabled-until-prompt) variant — the `{% if handoff %}`-guarded branches inside already handle a falsy handoff. Verify `compose_box` does not itself read `card.handoff`; if it does, pass it `card.handoffs | length > 0` for its collapsed state (grep the macro).

- [ ] **Step 5: Loop the block in the dashboard card partial**

In the dashboard/cards partial (the file grep found), replace the single `handoff_block(card, ...)` with `{% for h in card.handoffs %}{{ handoff_block(card, h, totals) }}{% endfor %}` so the card stacks every queued handoff inline (per the approved design). Match whatever wrapper/actions the dashboard variant used.

- [ ] **Step 6: Run the template tests to verify they pass**

Run: `uv run pytest tests/test_shell_contract.py -k "handoff or two_handoffs" -v`
Expected: PASS — both `data-handoff-section` ids present.

- [ ] **Step 7: Run the full template/contract suite for regressions**

Run: `uv run pytest tests/test_shell_contract.py -q`
Expected: PASS. A single-handoff card still renders exactly one block (the loop runs once).

- [ ] **Step 8: Commit**

```bash
git add src/bridge/templates/ tests/test_shell_contract.py
git commit -m "Render one fireable launch block per queued handoff"
```

---

### Task 6: Static JS — verify per-handoff wiring across multiple blocks

**Files:**
- Inspect/Modify: `src/bridge/static/launch.js`, `src/bridge/static/copy.js` (whichever bind `data-launch-handoff`, `data-prompt-handoff`, `data-handoff-dismiss`, `data-handoff-section`)
- Test: `tests/test_static_js.py`

**Interfaces:**
- Consumes: the per-block `data-*="<handoff-id>"` hooks from Task 5.
- Produces: no behaviour change per block — confirms the handlers select per-element (by the id in the attribute) rather than assuming one handoff per card, so two blocks operate independently.

- [ ] **Step 1: Write the failing/guarding test**

`tests/test_static_js.py` already asserts JS-source contracts (grep it for `data-launch-handoff`). Add a test asserting the launch handler resolves the handoff id **from the clicked element's own attribute**, not from a single per-card lookup — e.g. assert the source uses `querySelectorAll`/`closest` + `dataset.launchHandoff` rather than a single `querySelector('[data-launch-handoff]')`:

```python
def test_launch_binds_each_handoff_button(js_source):
    src = js_source("launch.js")
    # Must bind every launch button, not just the first.
    assert "querySelectorAll" in src
    # Must read the id off the button that fired, so stacked blocks stay independent.
    assert "dataset.launchHandoff" in src or "data-launch-handoff" in src
```

(Mirror the assertion style already in `tests/test_static_js.py`; the intent is "handlers are per-element".)

- [ ] **Step 2: Run the test to verify it fails or passes**

Run: `uv run pytest tests/test_static_js.py -k "each_handoff or launch_binds" -v`
Expected: If it FAILS, the handler used a single `querySelector` — proceed to Step 3. If it PASSES, the wiring is already per-element (it keys on `data-launch-handoff`); record that and skip to Step 4.

- [ ] **Step 3: Make the handlers per-element (only if Step 2 failed)**

Convert any `document.querySelector('[data-launch-handoff]')` (first-match) to `querySelectorAll(...)` with a per-button listener that reads `btn.dataset.launchHandoff`. Do the same for prompt-save (`data-prompt-handoff`), dismiss (`data-handoff-dismiss`), and schedule toggles if any used a single-match lookup. Keep each block's status spans keyed by that block's `hid`.

- [ ] **Step 4: Run the JS-contract suite**

Run: `uv run pytest tests/test_static_js.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bridge/static/ tests/test_static_js.py
git commit -m "Bind handoff controls per block so stacked handoffs fire independently"
```

---

### Task 7: Full-suite verification and manual smoke

**Files:** none (verification only).

- [ ] **Step 1: Run the entire suite**

Run: `uv run pytest -q`
Expected: PASS, count >= the 1025 at plan start plus the tests added here. Do NOT pipe to `head`/`tail` before reading the result.

- [ ] **Step 2: Manual smoke against a live panel**

The local panel restart is pre-authorized (never ask). Restart `bridge serve` on 8787, then create two handoffs for one project with distinct sessions:

```bash
bridge handoff --summary "Planned multi-handoff" --session-id smoke-1 --prompt-file - <<'P'
continue the planning thread
P
bridge handoff --summary "UI settings band" --session-id smoke-2 --prompt-file - <<'P'
continue the UI thread
P
```

Open http://127.0.0.1:8787 and confirm the project shows **two** stacked handoff blocks, each with its own summary and Run. Verify in Mit's actual browser (Arc, via osascript) — not the devtools Chrome — since rendering is what's being checked.

- [ ] **Step 3: Verify independent firing**

Fire one handoff from the panel; confirm the other remains queued and visible, and the fired one leaves the queue.

- [ ] **Step 4: Final commit if any smoke fix was needed**

```bash
git add -A
git commit -m "Fix issues found during multi-handoff smoke test"
```

(If smoke is clean, no commit — say so.)

---

## Self-Review Notes

- **Spec coverage:** session-scoped supersede (T1), null-session rule (T1), plural read (T1/T3), card list + compat property (T2), attention/counts (T4), stacked inline blocks + per-handoff Run (T5), launch fallback removal (T3), per-element JS (T6), migration-free (no task needed — none required), tests incl. one-handoff regression (T1 step 5, T5 step 7). All spec sections map to a task.
- **Type consistency:** `queued_handoffs` (list) / `queued_handoff` (newest) defined T1, consumed T2/T3/T4; `Card.handoffs` (list) / `Card.handoff` (property) defined T2, consumed T4/T5; macros gain an explicit `handoff` param T5, consumed by the loops in the same task.
- **Open verification the executor must resolve by grepping (named in-task):** exact fixture/helper names in each test file; the dashboard card partial filename; whether `compose_box` reads `card.handoff`; whether existing JS is already per-element (T6 Step 2 branches on it).
