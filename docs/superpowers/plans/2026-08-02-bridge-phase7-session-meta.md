# Phase 7 — session-meta enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface per-session activity facts (files/lines changed, commits/pushes, duration, friction, capability flags) from `~/.claude/usage-data/session-meta/*.json` on the project detail page's sessions table.

**Architecture:** A new pure-function component `sessionmeta.py` (mirrors `gitprobe.py`) reads a meta file by session id at request time, tolerating every absence. The `detail()` route reads meta for the sessions it already fetched, keyed off a new `Config.session_meta_dir`, and passes a `{id: SessionMeta}` map to the template, which renders a guarded "Changes" column. Nothing is persisted; the transcript parse remains the sole token authority.

**Tech Stack:** Python 3.13 (stdlib `json`, `dataclasses`, `pathlib`), FastAPI + Jinja2, plain CSS. Tests: pytest. Mutations: `tools/falsify.py`.

## Global Constraints

- **Never read session-meta's token fields.** `input_tokens`/`output_tokens` are absent from `SessionMeta` by design; page tokens come only from the `sessions` table. (spec constraint 1)
- **Never blocking, never an error.** Missing / malformed / mismatched meta → row renders as today, no raise, no 500. (spec constraint 2)
- **Never a total.** Per-row enrichment only; no aggregate across sessions. (spec constraint 3)
- **Tooling:** run tests with `/Users/mitsheth/.local/bin/uv run pytest -q`. Run mutations with `/Users/mitsheth/.local/bin/uv run python tools/falsify.py --spec tools/mutations/<file>.json` (needs a committed clean tree). Use absolute coreutil paths in shell.
- **No AI attribution** in any commit message.

---

### Task 1: `sessionmeta.py` reader component

**Files:**
- Create: `src/bridge/sessionmeta.py`
- Test: `tests/test_sessionmeta.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces:
  - `SessionMeta` frozen dataclass with int fields `files_modified, lines_added, lines_removed, git_commits, git_pushes, duration_minutes, tool_errors, user_interruptions` and bool fields `uses_task_agent, uses_mcp, uses_web`, plus a `has_signal: bool` property.
  - `read(session_id: str, meta_dir: Path = DEFAULT_META_DIR) -> SessionMeta | None`
  - `read_many(session_ids, meta_dir: Path = DEFAULT_META_DIR) -> dict[str, SessionMeta]`
  - `DEFAULT_META_DIR: Path`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sessionmeta.py
import json
from pathlib import Path

from bridge import sessionmeta


def _write(meta_dir: Path, session_id: str, **fields) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    body = {"session_id": session_id}
    body.update(fields)
    (meta_dir / f"{session_id}.json").write_text(json.dumps(body), encoding="utf-8")


def test_read_populates_every_surfaced_field(tmp_path):
    _write(tmp_path, "s1", files_modified=3, lines_added=120, lines_removed=40,
           git_commits=2, git_pushes=1, duration_minutes=45, tool_errors=1,
           user_interruptions=2, uses_task_agent=True, uses_mcp=True,
           uses_web_search=True, uses_web_fetch=False)
    m = sessionmeta.read("s1", tmp_path)
    assert m is not None
    assert (m.files_modified, m.lines_added, m.lines_removed) == (3, 120, 40)
    assert (m.git_commits, m.git_pushes, m.duration_minutes) == (2, 1, 45)
    assert (m.tool_errors, m.user_interruptions) == (1, 2)
    assert m.uses_task_agent and m.uses_mcp and m.uses_web


def test_read_never_carries_token_fields(tmp_path):
    # Constraint 1: the transcript parse is the sole token authority.
    _write(tmp_path, "s1", input_tokens=999, output_tokens=888, files_modified=1)
    m = sessionmeta.read("s1", tmp_path)
    assert not hasattr(m, "input_tokens")
    assert not hasattr(m, "output_tokens")


def test_uses_web_is_true_when_either_web_flag_is_set(tmp_path):
    _write(tmp_path, "a", uses_web_search=True)
    _write(tmp_path, "b", uses_web_fetch=True)
    _write(tmp_path, "c")
    assert sessionmeta.read("a", tmp_path).uses_web is True
    assert sessionmeta.read("b", tmp_path).uses_web is True
    assert sessionmeta.read("c", tmp_path).uses_web is False


def test_missing_file_returns_none(tmp_path):
    assert sessionmeta.read("nope", tmp_path) is None


def test_malformed_json_returns_none(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert sessionmeta.read("bad", tmp_path) is None


def test_mismatched_session_id_returns_none(tmp_path):
    # A renamed/corrupt file whose body names a different session.
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "s1.json").write_text(
        json.dumps({"session_id": "OTHER", "files_modified": 5}), encoding="utf-8")
    assert sessionmeta.read("s1", tmp_path) is None


def test_absent_keys_default_to_zero_and_false(tmp_path):
    _write(tmp_path, "s1")  # only session_id present
    m = sessionmeta.read("s1", tmp_path)
    assert m.files_modified == 0 and m.duration_minutes == 0
    assert m.uses_task_agent is False and m.uses_mcp is False and m.uses_web is False


def test_non_integer_field_is_tolerated_as_zero(tmp_path):
    _write(tmp_path, "s1", files_modified="lots")
    assert sessionmeta.read("s1", tmp_path).files_modified == 0


def test_has_signal_is_false_for_an_all_zero_session(tmp_path):
    _write(tmp_path, "s1")
    assert sessionmeta.read("s1", tmp_path).has_signal is False


def test_has_signal_is_true_when_any_fact_is_present(tmp_path):
    _write(tmp_path, "s1", duration_minutes=1)
    assert sessionmeta.read("s1", tmp_path).has_signal is True


def test_read_many_keeps_only_signal_bearing_ids(tmp_path):
    _write(tmp_path, "has", files_modified=2)
    _write(tmp_path, "empty")            # exists but no signal
    # "gone" has no file at all
    out = sessionmeta.read_many(["has", "empty", "gone"], tmp_path)
    assert set(out) == {"has"}
    assert out["has"].files_modified == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_sessionmeta.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'bridge.sessionmeta'`.

- [ ] **Step 3: Write the implementation**

```python
# src/bridge/sessionmeta.py
"""Opportunistic read of /insights session-meta files.

`~/.claude/usage-data/session-meta/{session_id}.json` is the `/insights`
output, capped at 200 sessions newest-first. It is enrichment only: a missing,
malformed, or mismatched file is the ordinary case for any session older than
the newest 200, and is never an error.

The token fields these files carry (`input_tokens`/`output_tokens`) are
DELIBERATELY not read. The transcript parse is Bridge's sole token authority;
a second, disagreeing number is the exact failure this module must not create.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_META_DIR = Path.home() / ".claude" / "usage-data" / "session-meta"


@dataclass(frozen=True)
class SessionMeta:
    files_modified: int
    lines_added: int
    lines_removed: int
    git_commits: int
    git_pushes: int
    duration_minutes: int
    tool_errors: int
    user_interruptions: int
    uses_task_agent: bool
    uses_mcp: bool
    uses_web: bool

    @property
    def has_signal(self) -> bool:
        return bool(
            self.files_modified or self.lines_added or self.lines_removed
            or self.git_commits or self.git_pushes or self.duration_minutes
            or self.tool_errors or self.user_interruptions
            or self.uses_task_agent or self.uses_mcp or self.uses_web
        )


def _int(raw: dict, key: str) -> int:
    try:
        return int(raw.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def read(session_id: str, meta_dir: Path = DEFAULT_META_DIR) -> SessionMeta | None:
    try:
        raw = json.loads(
            (Path(meta_dir) / f"{session_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("session_id") != session_id:
        return None
    return SessionMeta(
        files_modified=_int(raw, "files_modified"),
        lines_added=_int(raw, "lines_added"),
        lines_removed=_int(raw, "lines_removed"),
        git_commits=_int(raw, "git_commits"),
        git_pushes=_int(raw, "git_pushes"),
        duration_minutes=_int(raw, "duration_minutes"),
        tool_errors=_int(raw, "tool_errors"),
        user_interruptions=_int(raw, "user_interruptions"),
        uses_task_agent=bool(raw.get("uses_task_agent")),
        uses_mcp=bool(raw.get("uses_mcp")),
        uses_web=bool(raw.get("uses_web_search")) or bool(raw.get("uses_web_fetch")),
    )


def read_many(session_ids, meta_dir: Path = DEFAULT_META_DIR) -> dict[str, SessionMeta]:
    out: dict[str, SessionMeta] = {}
    for sid in session_ids:
        m = read(sid, meta_dir)
        if m is not None and m.has_signal:
            out[sid] = m
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_sessionmeta.py -q`
Expected: PASS (all 11 tests).

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add src/bridge/sessionmeta.py tests/test_sessionmeta.py
/usr/bin/git commit -m "Add sessionmeta reader for opportunistic /insights enrichment"
```

---

### Task 2: Wire enrichment into the detail page

**Files:**
- Modify: `src/bridge/config.py` (add `session_meta_dir` to `Config` dataclass ~line 174 and to `load()` ~line 197)
- Modify: `src/bridge/api.py` (import `sessionmeta`; `detail()` route ~line 741)
- Modify: `src/bridge/templates/project.html` (sessions table ~lines 69-90)
- Modify: `src/bridge/static/app.css` (new enrichment styles)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `sessionmeta.read_many(session_ids, meta_dir) -> dict[str, SessionMeta]` and `SessionMeta` (Task 1); `Config` (config.py); `store.sessions(project_id)` returning rows keyed `id`.
- Produces: template context key `session_metas: dict[str, SessionMeta]`; `Config.session_meta_dir: Path`.

- [ ] **Step 1: Write the failing config test**

```python
# tests/test_config.py  (add; create the file if absent)
from pathlib import Path

from bridge.config import load


def test_session_meta_dir_defaults_under_claude_usage_data():
    cfg = load()
    assert cfg.session_meta_dir == Path.home() / ".claude" / "usage-data" / "session-meta"


def test_session_meta_dir_is_overridable(tmp_path):
    cfg = load({"session_meta_dir": tmp_path / "meta"})
    assert cfg.session_meta_dir == tmp_path / "meta"
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_config.py -q`
Expected: FAIL — `Config` has no `session_meta_dir` (either `TypeError` on `replace` or `AttributeError`).

- [ ] **Step 3: Add the config field**

In `src/bridge/config.py`, add to the `Config` dataclass immediately after `claude_projects_dir: Path`:

```python
    session_meta_dir: Path
```

And in `load()`, immediately after the `claude_projects_dir=...` line:

```python
        session_meta_dir=home / ".claude" / "usage-data" / "session-meta",
```

- [ ] **Step 4: Run to verify the config test passes**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing detail-page tests**

```python
# tests/test_api.py  (append near the Phase 5 detail tests)

def _write_meta(cfg, session_id, **fields):
    d = cfg.session_meta_dir
    d.mkdir(parents=True, exist_ok=True)
    import json as _json
    body = {"session_id": session_id}
    body.update(fields)
    (d / f"{session_id}.json").write_text(_json.dumps(body), encoding="utf-8")


def test_detail_page_shows_session_meta_activity_when_present(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool",
                "session_meta_dir": tmp_path / "meta"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo", title="Worked",
                      ended_at="2026-07-30T10:00:00.000Z", tokens_in=5, tokens_out=5),
        pid)
    _write_meta(cfg, "s1", files_modified=3, lines_added=120, lines_removed=40,
                git_commits=2, git_pushes=1, duration_minutes=45,
                uses_task_agent=True, uses_mcp=True, uses_web_search=True)

    html = TestClient(create_app(store, cfg)).get(f"/project/{pid}").text

    assert "3 files" in html
    assert "+120" in html and "40" in html
    assert "2 commits" in html and "1 push" in html
    assert "45m" in html
    assert "agent" in html and "mcp" in html and "web" in html
    store.close()


def test_detail_page_omits_token_fields_from_meta(tmp_path):
    # Constraint 1: a meta file's token numbers must never reach the page.
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool",
                "session_meta_dir": tmp_path / "meta"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo", title="Worked",
                      ended_at="2026-07-30T10:00:00.000Z", tokens_in=5, tokens_out=5),
        pid)
    _write_meta(cfg, "s1", input_tokens=99999, output_tokens=88888, files_modified=1)

    html = TestClient(create_app(store, cfg)).get(f"/project/{pid}").text

    assert "99999" not in html and "88888" not in html
    store.close()


def test_detail_page_is_unchanged_when_meta_dir_is_empty(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool",
                "session_meta_dir": tmp_path / "meta"})  # never created
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo", title="Worked",
                      ended_at="2026-07-30T10:00:00.000Z", tokens_in=5, tokens_out=5),
        pid)

    r = TestClient(create_app(store, cfg)).get(f"/project/{pid}")

    assert r.status_code == 200
    assert "Worked" in r.text
    store.close()


def test_detail_page_survives_malformed_meta(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool",
                "session_meta_dir": tmp_path / "meta"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo", title="Worked",
                      ended_at="2026-07-30T10:00:00.000Z", tokens_in=5, tokens_out=5),
        pid)
    (tmp_path / "meta").mkdir(parents=True)
    (tmp_path / "meta" / "s1.json").write_text("{broken", encoding="utf-8")

    r = TestClient(create_app(store, cfg)).get(f"/project/{pid}")

    assert r.status_code == 200
    store.close()


def test_detail_page_hides_zero_activity_meta(tmp_path):
    # A meta file that records a pure Q&A session renders no activity.
    cfg = load({"db_path": tmp_path / "a.db", "spool_dir": tmp_path / "spool",
                "session_meta_dir": tmp_path / "meta"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo", title="Worked",
                      ended_at="2026-07-30T10:00:00.000Z", tokens_in=5, tokens_out=5),
        pid)
    _write_meta(cfg, "s1")  # session_id only, all facts zero

    html = TestClient(create_app(store, cfg)).get(f"/project/{pid}").text

    assert "files" not in html.split("<table")[-1] or "0 files" not in html
    store.close()
```

- [ ] **Step 6: Run to verify the detail tests fail**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_api.py -q -k "session_meta or detail_page"`
Expected: FAIL — the activity strings are absent (route/template not wired).

- [ ] **Step 7: Wire the route**

In `src/bridge/api.py`, add `sessionmeta` to the `bridge` import (the module already does `from bridge import launcher, spool, ...` near the top — add `sessionmeta` to that list). Then in `detail()`, replace the sessions fetch + context so the metas are read once:

```python
        sessions = store.sessions(project_id)
        session_metas = sessionmeta.read_many(
            [s["id"] for s in sessions], cfg.session_meta_dir
        )
        return templates.TemplateResponse(
            request,
            "project.html",
            {
                "project": row,
                "git": git,
                "sessions": sessions,
                "session_metas": session_metas,
                "handoffs": store.handoffs(project_id),
                "launches": store.launches(project_id),
            },
        )
```

- [ ] **Step 8: Wire the template**

In `src/bridge/templates/project.html`, add a header cell to the sessions table `<thead>` after `<th scope="col">Tokens</th>`:

```html
      <th scope="col">Changes</th>
```

And add this `<td>` inside the row loop, after the tokens `<td>` (before `</tr>`):

```html
      {% set m = session_metas.get(s["id"]) %}
      <td class="sessions__changes">
        {% if m %}
          {% if m.files_modified or m.lines_added or m.lines_removed %}
          <span class="sessions__diffstat">{% if m.files_modified %}{{ m.files_modified }} files{% endif %}{% if m.lines_added or m.lines_removed %} <span class="sessions__add">+{{ m.lines_added }}</span>/<span class="sessions__del">-{{ m.lines_removed }}</span>{% endif %}</span>
          {% endif %}
          {% set ns = namespace(facts=[]) %}
          {% if m.git_commits %}{% set ns.facts = ns.facts + [(m.git_commits | string) + (' commit' if m.git_commits == 1 else ' commits')] %}{% endif %}
          {% if m.git_pushes %}{% set ns.facts = ns.facts + [(m.git_pushes | string) + (' push' if m.git_pushes == 1 else ' pushes')] %}{% endif %}
          {% if m.duration_minutes %}{% set ns.facts = ns.facts + [(m.duration_minutes | string) + 'm'] %}{% endif %}
          {% if m.tool_errors %}{% set ns.facts = ns.facts + [(m.tool_errors | string) + (' tool error' if m.tool_errors == 1 else ' tool errors')] %}{% endif %}
          {% if m.user_interruptions %}{% set ns.facts = ns.facts + [(m.user_interruptions | string) + (' interruption' if m.user_interruptions == 1 else ' interruptions')] %}{% endif %}
          {% if ns.facts %}<span class="sessions__meta-detail">{{ ns.facts | join(' · ') }}</span>{% endif %}
          {% if m.uses_task_agent or m.uses_mcp or m.uses_web %}
          <span class="sessions__flags">{% if m.uses_task_agent %}<span class="badge">agent</span>{% endif %}{% if m.uses_mcp %}<span class="badge">mcp</span>{% endif %}{% if m.uses_web %}<span class="badge">web</span>{% endif %}</span>
          {% endif %}
        {% endif %}
      </td>
```

- [ ] **Step 9: Add minimal, accessible CSS**

In `src/bridge/static/app.css`, append (color is decorative — the `+`/`-` prefixes and `badge` text carry the meaning, satisfying WCAG 2.2 "not by color alone"):

```css
.sessions__meta-detail { display: block; font-size: .8rem; color: var(--muted); }
.sessions__add { color: #1a7f37; }
.sessions__del { color: #b3261e; }
.sessions__flags { display: inline-flex; gap: .25rem; margin-top: .2rem; }
.badge {
  font-size: .68rem; padding: .05rem .35rem; border-radius: .5rem;
  border: 1px solid var(--muted); color: var(--muted); white-space: nowrap;
}
```

- [ ] **Step 10: Run the detail tests, then the full suite**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_api.py tests/test_config.py -q`
Expected: PASS.
Run: `/Users/mitsheth/.local/bin/uv run pytest -q`
Expected: PASS (full suite green — no regression).

- [ ] **Step 11: Design-guardrails pass**

Invoke the `design-guardrails` skill on the `project.html` + `app.css` diff (the Changes column, the badges, the diffstat colors) before committing. Fix anything it flags — contrast on `#1a7f37`/`#b3261e` against both themes, badge legibility, and that meaning never rests on color alone. This is a required gate for UI work.

- [ ] **Step 12: Commit**

```bash
/usr/bin/git add src/bridge/config.py src/bridge/api.py src/bridge/templates/project.html src/bridge/static/app.css tests/test_api.py tests/test_config.py
/usr/bin/git commit -m "Enrich the detail page sessions table with /insights activity"
```

---

### Task 3: Pin the guards with a mutation spec

**Files:**
- Create: `tools/mutations/session-meta.json`

**Interfaces:**
- Consumes: the anchors in `src/bridge/sessionmeta.py` and the tests from Tasks 1–2.

Each mutation names a real defect and the test that must catch it. Anchors must
match their file exactly once (`test_mutation_specs.py` enforces this on the
ordinary suite). Follow the mutation-survivor discipline: a SURVIVED result is a
vacuous test until proven a genuine equivalent.

- [ ] **Step 1: Write the mutation spec**

```json
[
  {
    "name": "drop the session_id mismatch guard, so a renamed/corrupt file enriches the wrong session",
    "file": "src/bridge/sessionmeta.py",
    "old": "if not isinstance(raw, dict) or raw.get(\"session_id\") != session_id:",
    "new": "if not isinstance(raw, dict):",
    "tests": ["tests/test_sessionmeta.py::test_mismatched_session_id_returns_none"],
    "expect_count": 1
  },
  {
    "name": "collapse uses_web to web_search only, so a web_fetch-only session loses its flag",
    "file": "src/bridge/sessionmeta.py",
    "old": "uses_web=bool(raw.get(\"uses_web_search\")) or bool(raw.get(\"uses_web_fetch\")),",
    "new": "uses_web=bool(raw.get(\"uses_web_search\")),",
    "tests": ["tests/test_sessionmeta.py::test_uses_web_is_true_when_either_web_flag_is_set"],
    "expect_count": 1
  },
  {
    "name": "make read_many keep no-signal ids, so a pure Q&A session renders an empty Changes cell shell",
    "file": "src/bridge/sessionmeta.py",
    "old": "if m is not None and m.has_signal:",
    "new": "if m is not None:",
    "tests": ["tests/test_sessionmeta.py::test_read_many_keeps_only_signal_bearing_ids"],
    "expect_count": 1
  },
  {
    "name": "swallow a non-integer field as the raw value, so a bad field crashes int math downstream",
    "file": "src/bridge/sessionmeta.py",
    "old": "        return int(raw.get(key) or 0)\n    except (TypeError, ValueError):\n        return 0",
    "new": "        return raw.get(key) or 0\n    except (TypeError, ValueError):\n        return 0",
    "tests": ["tests/test_sessionmeta.py::test_non_integer_field_is_tolerated_as_zero"],
    "expect_count": 1
  },
  {
    "name": "drop OSError from read's except, so a missing meta file raises instead of degrading to None",
    "file": "src/bridge/sessionmeta.py",
    "old": "    except (OSError, ValueError):\n        return None",
    "new": "    except (ValueError,):\n        return None",
    "tests": ["tests/test_sessionmeta.py::test_missing_file_returns_none"],
    "expect_count": 1
  }
]
```

- [ ] **Step 2: Run the mutation harness (needs a committed clean tree)**

Run: `/Users/mitsheth/.local/bin/uv run python tools/falsify.py --spec tools/mutations/session-meta.json`
Expected: every mutation CAUGHT (5/5). If any SURVIVES, do not "fix" by fabricating an unreachable case — determine whether the test is vacuous (strengthen it) or the mutation is a genuine equivalent (drop it and document why, per the mutation-survivor discipline).

- [ ] **Step 3: Confirm the anchor-drift guard still passes**

Run: `/Users/mitsheth/.local/bin/uv run pytest tests/test_mutation_specs.py -q`
Expected: PASS — every anchor in the new spec matches its file exactly once.

- [ ] **Step 4: Commit**

```bash
/usr/bin/git add tools/mutations/session-meta.json
/usr/bin/git commit -m "Pin sessionmeta guards with a mutation spec"
```

---

## Self-Review

**Spec coverage:**
- Goal (surface activity on detail table) → Tasks 1–2. ✓
- Constraint 1 (no token fields) → `SessionMeta` omits them + `test_read_never_carries_token_fields` + `test_detail_page_omits_token_fields_from_meta`. ✓
- Constraint 2 (never blocking) → `read` catches OSError/ValueError + mismatch → None; `test_detail_page_is_unchanged_when_meta_dir_is_empty`, `test_detail_page_survives_malformed_meta`. ✓
- Constraint 3 (never a total) → per-row `read_many` only; no aggregate anywhere. ✓
- Request-time read, no persistence → route calls `read_many`; no store/schema change. ✓
- Component mirrors `gitprobe.py` → new flat `sessionmeta.py`, pure functions. ✓
- `has_signal` hides zero-activity meta → property + `test_has_signal_*` + `test_detail_page_hides_zero_activity_meta`. ✓
- `meta_dir` via `Config` → Task 2 config field + `test_session_meta_dir_*`. ✓
- Mutation spec pins the guards → Task 3. ✓
- design-guardrails gate → Task 2 Step 11. ✓
- Out-of-scope items (persistence, aggregates, drill-down, tool_counts/languages maps) → not implemented, as intended. ✓

**Placeholder scan:** No TBD/TODO; every code step carries real code. ✓

**Type consistency:** `SessionMeta` fields and `read`/`read_many`/`has_signal` signatures match across Tasks 1–3; template uses `session_metas` (the exact context key the route sets); `cfg.session_meta_dir` matches the Config field name. ✓
