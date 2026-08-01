# Bridge Phase 1 — Read-Only Panel Implementation Plan

> **STATUS 2026-08-01: Phase 1 shipped and is merged.** All ten tasks below are
> implemented. `phase1-read-only-panel` merged to `main` at `08d5eab` — 93 tests
> passing, 35 project cards from 421,480 parsed lines, 0 parse errors — and the
> branch is deleted. Path aliasing (`bed0b3a`) landed with it. The checkboxes are
> ticked to match.
>
> Falsification was still done by hand in this phase; `tools/falsify.py` and the
> recorded `tools/mutations/*.json` specs arrive in Phase 2, so nothing here has a
> committed mutation spec.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local web dashboard that indexes the existing 9,229 Claude Code transcripts and renders one card per project showing what the last session did, git state, and token burn.

**Architecture:** A single FastAPI process is the sole writer to a SQLite database in WAL mode. A streaming JSONL indexer turns `~/.claude/projects/**/*.jsonl` into typed `SessionRecord`s, tracking a byte offset per file so re-scans read only appended bytes. Read-only probes (git) are cached and allowed to fail without breaking a card. No launching, no handoff writes, no live updates — those are Phases 2–4.

**Tech Stack:** Python 3.13 (pinned via `uv`), FastAPI, Jinja2, plain CSS, SQLite (stdlib `sqlite3`), pytest.

**Spec:** `docs/superpowers/specs/2026-07-31-bridge-control-panel-design.md`

## Global Constraints

- Python **3.13**, pinned via `uv`. `/usr/bin/python3` is 3.9.6 and must never be used. Every command is `uv run …`.
- Runtime dependencies limited to: `fastapi`, `uvicorn[standard]`, `jinja2`. Dev: `pytest`. Nothing else without a stated reason. **[2026-08-01: the runtime three held exactly. Dev gained one, `httpx2>=2.9.1`, and the reason was never written down — it is what `fastapi.testclient.TestClient` needs, and this plan mandates `TestClient` from Task 9 onward. Recorded here so it stops looking like drift.]**
- **Bridge never writes to a user project repo.** Its only writes are its own SQLite DB under `~/.bridge/`.
- **All git invocations are read-only**, use the absolute path `/usr/bin/git`, and carry a **2.0 second** timeout.
- **Bind to `127.0.0.1:8787` only.** No authentication, no `0.0.0.0`.
- SQLite: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`. Migrations are **additive only** — new columns and tables, never a table rebuild.
- **No probe failure may prevent the dashboard from rendering.** A card with every probe failed still renders.
- Absolute coreutil paths in any shell-out (`/usr/bin/git`, `/bin/ls`).
- WCAG 2.2 AA on all UI: contrast on every text/background pair, status never by color alone, full keyboard operation, visible focus rings.
- Real data facts the code must satisfy: `gitBranch` may be the literal `"HEAD"`; ~43% of project paths are **not** git repos; `attachment` records are ~62% of all JSONL records; unknown `type` values and absent keys are normal, not errors.

---

### Task 1: Project scaffold and config

**Files:**
- Create: `.python-version`, `pyproject.toml`
- Create: `src/bridge/__init__.py`, `src/bridge/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `bridge.config.Config` dataclass with fields `claude_projects_dir: Path`, `db_path: Path`, `dev_dir: Path`, `stale_hours: int`, `models: list[str]`, `efforts: list[str]`, `port: int`. Function `bridge.config.load(overrides: dict | None = None) -> Config`.

- [x] **Step 1: Create the Python pin and project metadata**

`.python-version`:
```
3.13
```

`pyproject.toml`:
```toml
[project]
name = "bridge"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["fastapi", "uvicorn[standard]", "jinja2"]

[project.optional-dependencies]
dev = ["pytest"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/bridge"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [x] **Step 2: Sync the environment**

Run: `cd ~/dev/bridge && uv sync --extra dev`
Expected: creates `.venv` with Python 3.13.x, installs fastapi/uvicorn/jinja2/pytest.

- [x] **Step 3: Write the failing test**

`tests/test_config.py`:
```python
from pathlib import Path

from bridge.config import Config, load


def test_load_returns_defaults():
    cfg = load()
    assert cfg.claude_projects_dir == Path.home() / ".claude" / "projects"
    assert cfg.db_path == Path.home() / ".bridge" / "bridge.db"
    assert cfg.stale_hours == 12
    assert cfg.port == 8787
    assert "high" in cfg.efforts
    assert cfg.models  # non-empty


def test_overrides_win(tmp_path):
    cfg = load({"db_path": tmp_path / "x.db", "stale_hours": 3})
    assert cfg.db_path == tmp_path / "x.db"
    assert cfg.stale_hours == 3
    # unspecified fields keep defaults
    assert cfg.port == 8787


def test_config_is_frozen():
    cfg = load()
    try:
        cfg.stale_hours = 99
    except Exception:
        return
    raise AssertionError("Config must be immutable")
```

- [x] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bridge.config'`

- [x] **Step 5: Write minimal implementation**

`src/bridge/__init__.py`:
```python
```
(empty file)

`src/bridge/config.py`:
```python
"""Configuration for Bridge. Overrides are for tests; there is no config file yet."""

from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_MODELS = ["opus", "sonnet", "haiku"]
DEFAULT_EFFORTS = ["low", "medium", "high"]


@dataclass(frozen=True)
class Config:
    claude_projects_dir: Path
    db_path: Path
    dev_dir: Path
    stale_hours: int
    models: list[str]
    efforts: list[str]
    port: int


def load(overrides: dict | None = None) -> Config:
    home = Path.home()
    cfg = Config(
        claude_projects_dir=home / ".claude" / "projects",
        db_path=home / ".bridge" / "bridge.db",
        dev_dir=home / "dev",
        stale_hours=12,
        models=list(DEFAULT_MODELS),
        efforts=list(DEFAULT_EFFORTS),
        port=8787,
    )
    if overrides:
        cfg = replace(cfg, **overrides)
    return cfg
```

- [x] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 3 passed

- [x] **Step 7: Commit**

```bash
cd ~/dev/bridge
git add .python-version pyproject.toml uv.lock src/bridge/__init__.py src/bridge/config.py tests/test_config.py
git commit -m "Add project scaffold and configuration"
```

---

### Task 2: Transcript parsing into SessionRecord

**Files:**
- Create: `src/bridge/models.py`, `src/bridge/transcripts.py`
- Create: `tests/conftest.py`, `tests/test_transcripts.py`
- Test: `tests/test_transcripts.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `bridge.models.SessionRecord` dataclass: `session_id: str`, `project_path: str | None`, `title: str | None`, `started_at: str | None`, `ended_at: str | None`, `model: str | None`, `effort: str | None`, `git_branch: str | None`, `user_msgs: int`, `assistant_msgs: int`, `last_prompt: str | None`, `tokens_in: int`, `tokens_out: int`, `tokens_cache_create: int`, `tokens_cache_read: int`, `sidechain_tokens: int`, `interrupted: bool`, `transcript_path: str`.
  - `bridge.transcripts.ScanResult` dataclass: `record: SessionRecord | None`, `new_offset: int`, `lines_parsed: int`, `parse_errors: int`.
  - `bridge.transcripts.scan(path: Path, start_offset: int = 0, prev: SessionRecord | None = None) -> ScanResult`

- [x] **Step 1: Write the fixture builder**

`tests/conftest.py`:
```python
import json
from pathlib import Path

import pytest


def jline(**kw) -> str:
    return json.dumps(kw) + "\n"


@pytest.fixture
def write_transcript(tmp_path):
    """Write JSONL lines to a file and return its path."""

    def _write(name: str, lines: list[str]) -> Path:
        p = tmp_path / name
        p.write_text("".join(lines))
        return p

    return _write


@pytest.fixture
def normal_session():
    """A realistic minimal session: title, two turns, usage, cwd, branch."""
    sid = "11111111-1111-1111-1111-111111111111"
    return sid, [
        jline(type="last-prompt", leafUuid="a", sessionId=sid),
        jline(
            type="user", sessionId=sid, isSidechain=False,
            timestamp="2026-07-30T10:00:00.000Z",
            cwd="/Users/mitsheth/dev/demo", gitBranch="main",
            message={"role": "user", "content": "do the thing"},
        ),
        jline(
            type="assistant", sessionId=sid, isSidechain=False,
            timestamp="2026-07-30T10:00:05.000Z",
            cwd="/Users/mitsheth/dev/demo", gitBranch="main", effort="high",
            message={
                "role": "assistant", "model": "claude-opus-5",
                "usage": {
                    "input_tokens": 10, "output_tokens": 20,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 40,
                },
            },
        ),
        jline(type="ai-title", sessionId=sid, aiTitle="Do the thing"),
        jline(type="last-prompt", sessionId=sid, lastPrompt="do the thing again"),
    ]
```

- [x] **Step 2: Write the failing tests**

`tests/test_transcripts.py`:
```python
from bridge.transcripts import scan
from tests.conftest import jline


def test_parses_normal_session(write_transcript, normal_session):
    sid, lines = normal_session
    p = write_transcript("s.jsonl", lines)
    r = scan(p)
    rec = r.record
    assert rec.session_id == sid
    assert rec.project_path == "/Users/mitsheth/dev/demo"
    assert rec.title == "Do the thing"
    assert rec.last_prompt == "do the thing again"
    assert rec.git_branch == "main"
    assert rec.model == "claude-opus-5"
    assert rec.effort == "high"
    assert rec.user_msgs == 1
    assert rec.assistant_msgs == 1
    assert (rec.tokens_in, rec.tokens_out) == (10, 20)
    assert (rec.tokens_cache_create, rec.tokens_cache_read) == (30, 40)
    assert rec.started_at == "2026-07-30T10:00:00.000Z"
    assert rec.ended_at == "2026-07-30T10:00:05.000Z"
    assert rec.interrupted is False
    assert r.parse_errors == 0
    assert r.new_offset == p.stat().st_size


def test_malformed_line_is_counted_not_fatal(write_transcript, normal_session):
    sid, lines = normal_session
    p = write_transcript("s.jsonl", lines[:2] + ["{not json\n"] + lines[2:])
    r = scan(p)
    assert r.parse_errors == 1
    assert r.record.title == "Do the thing"  # scan continued past the bad line


def test_unknown_types_are_ignored(write_transcript, normal_session):
    sid, lines = normal_session
    extra = jline(type="totally-new-record-type", sessionId=sid, whatever=1)
    p = write_transcript("s.jsonl", lines + [extra])
    r = scan(p)
    assert r.parse_errors == 0
    assert r.record.session_id == sid


def test_missing_keys_tolerated(write_transcript):
    p = write_transcript("s.jsonl", [jline(type="assistant", sessionId="s2")])
    r = scan(p)
    assert r.record.session_id == "s2"
    assert r.record.model is None
    assert r.record.tokens_in == 0


def test_empty_file_yields_no_record(write_transcript):
    p = write_transcript("empty.jsonl", [])
    r = scan(p)
    assert r.record is None
    assert r.new_offset == 0


def test_truncated_final_line_is_not_an_error(write_transcript, normal_session):
    sid, lines = normal_session
    p = write_transcript("s.jsonl", lines)
    with p.open("a") as f:
        f.write('{"type":"assis')  # session still being written
    r = scan(p)
    assert r.parse_errors == 0
    # offset stops before the partial line so the next scan re-reads it whole
    assert r.new_offset == sum(len(x.encode()) for x in lines)


def test_detached_head_branch_literal(write_transcript):
    p = write_transcript("s.jsonl", [
        jline(type="user", sessionId="s3", isSidechain=False,
              timestamp="2026-07-30T10:00:00.000Z",
              cwd="/tmp/x", gitBranch="HEAD",
              message={"role": "user", "content": "hi"}),
    ])
    assert scan(p).record.git_branch == "HEAD"


def test_attachment_records_excluded_from_counts(write_transcript):
    p = write_transcript("s.jsonl", [
        jline(type="attachment", sessionId="s4", attachment={"big": "x" * 100}),
        jline(type="user", sessionId="s4", isSidechain=False,
              message={"role": "user", "content": "hi"}),
    ])
    rec = scan(p).record
    assert rec.user_msgs == 1
    assert rec.assistant_msgs == 0


def test_sidechain_tokens_tracked_separately(write_transcript):
    p = write_transcript("s.jsonl", [
        jline(type="assistant", sessionId="s5", isSidechain=True,
              message={"role": "assistant", "model": "claude-haiku-4-5",
                       "usage": {"input_tokens": 5, "output_tokens": 7}}),
    ])
    rec = scan(p).record
    assert rec.sidechain_tokens == 12
    assert rec.tokens_in == 0  # not counted in main totals
    assert rec.assistant_msgs == 0  # sidechain turns are not the session's turns


def test_interrupted_session_flagged(write_transcript):
    p = write_transcript("s.jsonl", [
        jline(type="assistant", sessionId="s6", isSidechain=False,
              interruptedByShutdown=True, message={"role": "assistant"}),
    ])
    assert scan(p).record.interrupted is True
```

- [x] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_transcripts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bridge.transcripts'`

- [x] **Step 4: Write the implementation**

`src/bridge/models.py`:
```python
from dataclasses import dataclass, field


@dataclass
class SessionRecord:
    session_id: str
    transcript_path: str
    project_path: str | None = None
    title: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    model: str | None = None
    effort: str | None = None
    git_branch: str | None = None
    user_msgs: int = 0
    assistant_msgs: int = 0
    last_prompt: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache_create: int = 0
    tokens_cache_read: int = 0
    sidechain_tokens: int = 0
    interrupted: bool = False


@dataclass
class GitState:
    """status is the discriminator: ok | not_a_repo | unavailable."""

    status: str
    branch: str | None = None
    dirty_count: int = 0
    ahead: int | None = None
    behind: int | None = None
    last_commit_summary: str | None = None
    last_commit_at: int | None = None
    oldest_uncommitted_at: int | None = None


@dataclass
class Card:
    project_id: int
    path: str
    name: str
    session: SessionRecord | None
    git: GitState
    tokens_today: int
    tokens_5h: int
    spark: list[int] = field(default_factory=list)
    is_stale: bool = False
```

`src/bridge/transcripts.py`:
```python
"""Stream Claude Code JSONL transcripts into SessionRecords.

Design notes driven by the real corpus (9,229 files, 3.5 GB):
  * `attachment` records are ~62% of all lines and carry nothing we need, so
    `_apply` ignores them by `type`. A byte-prefix fast path was tried and
    removed: measured against the real corpus it never matched once across
    13,796 attachment records, because the attachment payload precedes the
    record's own `type` key on the line.
  * Unknown `type` values and absent keys are normal across CLI versions.
  * A truncated final line means the session is still being written. It is not
    an error, and the returned offset stops before it so the next scan re-reads
    it whole.
"""

import json
from dataclasses import dataclass, replace
from pathlib import Path

from bridge.models import SessionRecord


@dataclass
class ScanResult:
    record: SessionRecord | None
    new_offset: int
    lines_parsed: int = 0
    parse_errors: int = 0


def scan(path: Path, start_offset: int = 0, prev: SessionRecord | None = None) -> ScanResult:
    """Read `path` from `start_offset` to the last complete line.

    `prev` lets an incremental scan accumulate onto an earlier result.
    """
    rec = replace(prev) if prev else None
    offset = start_offset
    lines_parsed = 0
    parse_errors = 0

    with path.open("rb") as f:
        f.seek(start_offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # partial trailing line; leave offset before it
            offset += len(raw)
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                parse_errors += 1
                continue
            if not isinstance(obj, dict):
                parse_errors += 1
                continue
            lines_parsed += 1
            rec = _apply(rec, obj, str(path))

    return ScanResult(rec, offset, lines_parsed, parse_errors)


def _apply(rec: SessionRecord | None, obj: dict, path: str) -> SessionRecord | None:
    sid = obj.get("sessionId") or obj.get("session_id")
    if rec is None:
        if not sid:
            return None
        rec = SessionRecord(session_id=sid, transcript_path=path)

    kind = obj.get("type")
    if kind == "attachment":
        return rec
    if kind == "ai-title":
        rec.title = obj.get("aiTitle") or rec.title
        return rec
    if kind == "last-prompt":
        rec.last_prompt = obj.get("lastPrompt") or rec.last_prompt
        return rec

    if obj.get("cwd"):
        rec.project_path = obj["cwd"]
    if obj.get("gitBranch"):
        rec.git_branch = obj["gitBranch"]
    ts = obj.get("timestamp")
    if ts:
        if rec.started_at is None or ts < rec.started_at:
            rec.started_at = ts
        if rec.ended_at is None or ts > rec.ended_at:
            rec.ended_at = ts
    if obj.get("interruptedByShutdown") or obj.get("isAbortedMidStream"):
        rec.interrupted = True

    sidechain = bool(obj.get("isSidechain"))
    if kind == "user" and not sidechain:
        rec.user_msgs += 1
    elif kind == "assistant":
        msg = obj.get("message") or {}
        usage = msg.get("usage") or {}
        if sidechain:
            rec.sidechain_tokens += int(usage.get("input_tokens") or 0) + int(
                usage.get("output_tokens") or 0
            )
            return rec
        rec.assistant_msgs += 1
        if msg.get("model"):
            rec.model = msg["model"]
        if obj.get("effort"):
            rec.effort = obj["effort"]
        rec.tokens_in += int(usage.get("input_tokens") or 0)
        rec.tokens_out += int(usage.get("output_tokens") or 0)
        rec.tokens_cache_create += int(usage.get("cache_creation_input_tokens") or 0)
        rec.tokens_cache_read += int(usage.get("cache_read_input_tokens") or 0)
    return rec
```

- [x] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_transcripts.py -v`
Expected: 10 passed

- [x] **Step 6: Commit**

```bash
cd ~/dev/bridge
git add src/bridge/models.py src/bridge/transcripts.py tests/conftest.py tests/test_transcripts.py
git commit -m "Add tolerant streaming transcript parser"
```

---

### Task 3: Incremental rescan guarantee

**Files:**
- Modify: `tests/test_transcripts.py` (append)
- Test: `tests/test_transcripts.py`

**Interfaces:**
- Consumes: `bridge.transcripts.scan`, `ScanResult.lines_parsed`, `ScanResult.new_offset`, `SessionRecord` from Task 2.
- Produces: no new API. This task proves the performance *shape* the whole design depends on.

Rationale: a wall-clock assertion would pass on a fast machine even if the indexer secretly re-read all 3.5 GB. Asserting *how many lines were parsed* catches the actual regression.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_transcripts.py`:
```python
def test_rescan_with_no_changes_parses_nothing(write_transcript, normal_session):
    sid, lines = normal_session
    p = write_transcript("s.jsonl", lines)
    first = scan(p)
    again = scan(p, start_offset=first.new_offset, prev=first.record)
    assert again.lines_parsed == 0
    assert again.new_offset == first.new_offset
    assert again.record.title == "Do the thing"


def test_rescan_parses_only_appended_lines(write_transcript, normal_session):
    sid, lines = normal_session
    p = write_transcript("s.jsonl", lines)
    first = scan(p)
    with p.open("a") as f:
        f.write(jline(type="ai-title", sessionId=sid, aiTitle="Renamed"))
        f.write(jline(type="user", sessionId=sid, isSidechain=False,
                      message={"role": "user", "content": "more"}))
    second = scan(p, start_offset=first.new_offset, prev=first.record)
    assert second.lines_parsed == 2          # not len(lines) + 2
    assert second.record.title == "Renamed"  # accumulated onto prev
    assert second.record.user_msgs == 2


def test_incremental_totals_match_full_scan(write_transcript):
    """Accumulation across the offset boundary must not lose or double-count.

    BOTH halves must carry a user turn and an assistant turn with tokens. A
    fixture whose asserted fields are fed only by the post-offset half passes
    even when `prev` is discarded entirely, proving nothing.
    """
    sid = "44444444-4444-4444-4444-444444444444"
    cwd = "/Users/mitsheth/dev/demo"

    def user(ts, text):
        return jline(type="user", sessionId=sid, isSidechain=False,
                     timestamp=ts, cwd=cwd, gitBranch="main",
                     message={"role": "user", "content": text})

    def assistant(ts, tin, tout):
        return jline(type="assistant", sessionId=sid, isSidechain=False,
                     timestamp=ts, cwd=cwd,
                     message={"role": "assistant", "model": "claude-opus-5",
                              "usage": {"input_tokens": tin, "output_tokens": tout}})

    first = [user("2026-07-30T10:00:00.000Z", "one"),
             assistant("2026-07-30T10:00:01.000Z", 1, 2)]
    second = [user("2026-07-30T10:00:02.000Z", "two"),
              assistant("2026-07-30T10:00:03.000Z", 10, 20),
              jline(type="ai-title", sessionId=sid, aiTitle="Both halves")]

    p = write_transcript("s.jsonl", first)
    partial = scan(p)
    # The pre-offset half must really carry totals, or this test decays again.
    assert partial.record.user_msgs == 1
    assert partial.record.tokens_in == 1

    with p.open("a") as f:
        f.write("".join(second))

    incremental = scan(p, start_offset=partial.new_offset, prev=partial.record)
    full = scan(p)

    for field in ("user_msgs", "assistant_msgs", "tokens_in", "tokens_out",
                  "title", "started_at", "ended_at"):
        assert getattr(incremental.record, field) == getattr(full.record, field), field

    # State the arithmetic being protected, so a regression names itself.
    assert full.record.user_msgs == 2
    assert full.record.assistant_msgs == 2
    assert full.record.tokens_in == 11
    assert full.record.tokens_out == 22
    assert incremental.lines_parsed == 3
```

- [x] **Step 2: Run tests**

Run: `uv run pytest tests/test_transcripts.py -v`
Expected: 15 passed. The Task 2 implementation already satisfies these; if any fail, the accumulation logic in `scan`/`_apply` is wrong and must be fixed here rather than in a later task.

- [x] **Step 3: Commit**

```bash
cd ~/dev/bridge
git add tests/test_transcripts.py
git commit -m "Assert incremental rescan reads only appended lines"
```

---

### Task 4: SQLite store

**Files:**
- Create: `src/bridge/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `bridge.models.SessionRecord` (Task 2).
- Produces: `bridge.store.Store` with methods:
  - `Store(db_path: Path)` — creates parent dirs, applies schema, sets WAL.
  - `.upsert_project(path: str, name: str) -> int` (returns project id)
  - `.set_project_status(project_id: int, status: str) -> None`
  - `.projects(include_hidden: bool = False) -> list[sqlite3.Row]`
  - `.upsert_session(rec: SessionRecord, project_id: int) -> None`
  - `.latest_session(project_id: int) -> sqlite3.Row | None`
  - `.sessions(project_id: int, limit: int = 50) -> list[sqlite3.Row]`
  - `.get_scan_state(path: str) -> sqlite3.Row | None`
  - `.set_scan_state(path: str, size: int, mtime: float, offset: int, session_id: str) -> None`
  - `.token_totals(project_id: int, since_epoch: int) -> int`
  - `.close() -> None`

- [x] **Step 1: Write the failing tests**

`tests/test_store.py`:
```python
import sqlite3
import threading

import pytest

from bridge.models import SessionRecord
from bridge.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "sub" / "t.db")
    yield s
    s.close()


def rec(sid="s1", **kw):
    base = dict(
        session_id=sid, transcript_path=f"/t/{sid}.jsonl",
        project_path="/Users/mitsheth/dev/demo", title="T",
        started_at="2026-07-30T10:00:00.000Z", ended_at="2026-07-30T11:00:00.000Z",
        model="claude-opus-5", effort="high", git_branch="main",
        user_msgs=1, assistant_msgs=2, tokens_in=5, tokens_out=6,
    )
    base.update(kw)
    return SessionRecord(**base)


def test_wal_and_foreign_keys_enabled(store):
    assert store.conn.execute("pragma journal_mode").fetchone()[0].lower() == "wal"
    assert store.conn.execute("pragma foreign_keys").fetchone()[0] == 1


def test_creates_parent_directory(tmp_path):
    s = Store(tmp_path / "deep" / "nested" / "b.db")
    assert (tmp_path / "deep" / "nested" / "b.db").exists()
    s.close()


def test_upsert_project_is_idempotent(store):
    a = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    b = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    assert a == b
    assert len(store.projects()) == 1


def test_hidden_projects_excluded_by_default(store):
    pid = store.upsert_project("/x", "x")
    store.set_project_status(pid, "hidden")
    assert store.projects() == []
    assert len(store.projects(include_hidden=True)) == 1


def test_upsert_session_is_idempotent_and_updates(store):
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(rec(), pid)
    store.upsert_session(rec(title="Updated", tokens_out=99), pid)
    rows = store.sessions(pid)
    assert len(rows) == 1
    assert rows[0]["title"] == "Updated"
    assert rows[0]["tokens_out"] == 99


def test_latest_session_is_most_recent_by_ended_at(store):
    pid = store.upsert_project("/d", "d")
    store.upsert_session(rec("old", ended_at="2026-07-01T00:00:00.000Z"), pid)
    store.upsert_session(rec("new", ended_at="2026-07-30T00:00:00.000Z"), pid)
    assert store.latest_session(pid)["id"] == "new"


def test_scan_state_roundtrip(store):
    assert store.get_scan_state("/t/a.jsonl") is None
    store.set_scan_state("/t/a.jsonl", 100, 1.5, 90, "s1")
    row = store.get_scan_state("/t/a.jsonl")
    assert (row["size"], row["parsed_offset"], row["session_id"]) == (100, 90, "s1")
    store.set_scan_state("/t/a.jsonl", 200, 2.5, 190, "s1")
    assert store.get_scan_state("/t/a.jsonl")["parsed_offset"] == 190


def test_token_totals_respects_since(store):
    pid = store.upsert_project("/d", "d")
    store.upsert_session(rec("a", ended_at="2026-07-30T10:00:00.000Z",
                             tokens_in=10, tokens_out=10), pid)
    store.upsert_session(rec("b", ended_at="2026-01-01T00:00:00.000Z",
                             tokens_in=99, tokens_out=99), pid)
    # 2026-07-30T00:00:00Z == 1785369600
    assert store.token_totals(pid, 1785369600) == 20


def test_concurrent_writers_do_not_error(tmp_path):
    """Proves the WAL + busy_timeout assumption the architecture rests on."""
    db = tmp_path / "c.db"
    main = Store(db)
    pid = main.upsert_project("/d", "d")
    errors: list[Exception] = []

    def worker(n: int):
        s = Store(db)
        try:
            for i in range(20):
                s.upsert_session(rec(f"s{n}-{i}"), pid)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(main.sessions(pid, limit=1000)) == 80
    main.close()


def test_additive_migration_preserves_data(tmp_path):
    db = tmp_path / "m.db"
    s = Store(db)
    pid = s.upsert_project("/d", "d")
    s.upsert_session(rec(), pid)
    s.close()
    # Re-opening applies migrations against a populated DB without loss.
    s2 = Store(db)
    assert len(s2.sessions(pid)) == 1
    s2.close()
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bridge.store'`

- [x] **Step 3: Write the implementation**

`src/bridge/store.py`:
```python
"""SQLite persistence. The server process is the sole writer.

Migrations are additive only: append to SCHEMA, never rebuild a table.
`upsert_session` stores `ended_at` twice — once as the raw ISO string for
display, once as an epoch int so range queries stay index-friendly.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bridge.models import SessionRecord

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        pinned INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        title TEXT,
        started_at TEXT,
        ended_at TEXT,
        ended_epoch INTEGER,
        model TEXT,
        effort TEXT,
        git_branch TEXT,
        user_msgs INTEGER NOT NULL DEFAULT 0,
        assistant_msgs INTEGER NOT NULL DEFAULT 0,
        last_prompt TEXT,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        tokens_cache_create INTEGER NOT NULL DEFAULT 0,
        tokens_cache_read INTEGER NOT NULL DEFAULT 0,
        sidechain_tokens INTEGER NOT NULL DEFAULT 0,
        interrupted INTEGER NOT NULL DEFAULT 0,
        transcript_path TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, ended_epoch)",
    """
    CREATE TABLE IF NOT EXISTS scan_state (
        transcript_path TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        parsed_offset INTEGER NOT NULL,
        session_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS git_cache (
        project_id INTEGER PRIMARY KEY REFERENCES projects(id),
        payload_json TEXT NOT NULL,
        probed_at INTEGER NOT NULL
    )
    """,
]


def to_epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class Store:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        for stmt in SCHEMA:
            self.conn.execute(stmt)

    def close(self) -> None:
        self.conn.close()

    def upsert_project(self, path: str, name: str) -> int:
        self.conn.execute(
            "INSERT INTO projects(path, name, created_at) VALUES(?,?,?) "
            "ON CONFLICT(path) DO NOTHING",
            (path, name, now_epoch()),
        )
        return self.conn.execute(
            "SELECT id FROM projects WHERE path=?", (path,)
        ).fetchone()["id"]

    def set_project_status(self, project_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE projects SET status=? WHERE id=?", (status, project_id)
        )

    def projects(self, include_hidden: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM projects"
        if not include_hidden:
            sql += " WHERE status='active'"
        return list(self.conn.execute(sql + " ORDER BY name"))

    def upsert_session(self, rec: SessionRecord, project_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions (
                id, project_id, title, started_at, ended_at, ended_epoch, model,
                effort, git_branch, user_msgs, assistant_msgs, last_prompt,
                tokens_in, tokens_out, tokens_cache_create, tokens_cache_read,
                sidechain_tokens, interrupted, transcript_path
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, started_at=excluded.started_at,
                ended_at=excluded.ended_at, ended_epoch=excluded.ended_epoch,
                model=excluded.model, effort=excluded.effort,
                git_branch=excluded.git_branch, user_msgs=excluded.user_msgs,
                assistant_msgs=excluded.assistant_msgs,
                last_prompt=excluded.last_prompt, tokens_in=excluded.tokens_in,
                tokens_out=excluded.tokens_out,
                tokens_cache_create=excluded.tokens_cache_create,
                tokens_cache_read=excluded.tokens_cache_read,
                sidechain_tokens=excluded.sidechain_tokens,
                interrupted=excluded.interrupted,
                transcript_path=excluded.transcript_path
            """,
            (
                rec.session_id, project_id, rec.title, rec.started_at, rec.ended_at,
                to_epoch(rec.ended_at), rec.model, rec.effort, rec.git_branch,
                rec.user_msgs, rec.assistant_msgs, rec.last_prompt, rec.tokens_in,
                rec.tokens_out, rec.tokens_cache_create, rec.tokens_cache_read,
                rec.sidechain_tokens, int(rec.interrupted), rec.transcript_path,
            ),
        )

    def latest_session(self, project_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE project_id=? "
            "ORDER BY ended_epoch DESC NULLS LAST LIMIT 1",
            (project_id,),
        ).fetchone()

    def sessions(self, project_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM sessions WHERE project_id=? "
                "ORDER BY ended_epoch DESC NULLS LAST LIMIT ?",
                (project_id, limit),
            )
        )

    def get_scan_state(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scan_state WHERE transcript_path=?", (path,)
        ).fetchone()

    def set_scan_state(
        self, path: str, size: int, mtime: float, offset: int, session_id: str | None
    ) -> None:
        self.conn.execute(
            "INSERT INTO scan_state(transcript_path, size, mtime, parsed_offset, session_id) "
            "VALUES(?,?,?,?,?) ON CONFLICT(transcript_path) DO UPDATE SET "
            "size=excluded.size, mtime=excluded.mtime, "
            "parsed_offset=excluded.parsed_offset, session_id=excluded.session_id",
            (path, size, mtime, offset, session_id),
        )

    def token_totals(self, project_id: int, since_epoch: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(tokens_in + tokens_out),0) AS t FROM sessions "
            "WHERE project_id=? AND ended_epoch >= ?",
            (project_id, since_epoch),
        ).fetchone()
        return row["t"]
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: 10 passed

- [x] **Step 5: Commit**

```bash
cd ~/dev/bridge
git add src/bridge/store.py tests/test_store.py
git commit -m "Add SQLite store with WAL and additive schema"
```

---

### Task 5: Git probe

**Files:**
- Create: `src/bridge/gitprobe.py`
- Test: `tests/test_gitprobe.py`

**Interfaces:**
- Consumes: `bridge.models.GitState` (Task 2).
- Produces: `bridge.gitprobe.probe(path: Path, timeout: float = 2.0) -> GitState`.

`GitState.status` is the discriminator the template branches on:
- `"ok"` — a git repo was read successfully.
- `"not_a_repo"` — **the ~43% common case.** No warning treatment applies; the card shows a neutral note.
- `"unavailable"` — git timed out, is missing, or the path does not exist.

- [x] **Step 1: Write the failing tests**

`tests/test_gitprobe.py`:
```python
import subprocess

import pytest

from bridge.gitprobe import probe

GIT = "/usr/bin/git"


def run(cwd, *args):
    subprocess.run([GIT, *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    run(d, "init", "-q")
    run(d, "config", "user.name", "t")
    run(d, "config", "user.email", "t@t")
    (d / "a.txt").write_text("hello\n")
    run(d, "add", "a.txt")
    run(d, "commit", "-q", "-m", "first commit")
    return d


def test_clean_repo(repo):
    g = probe(repo)
    assert g.status == "ok"
    assert g.branch in ("main", "master")
    assert g.dirty_count == 0
    assert g.last_commit_summary == "first commit"
    assert isinstance(g.last_commit_at, int)
    assert g.oldest_uncommitted_at is None


def test_dirty_repo_counts_and_ages(repo):
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").write_text("new\n")
    g = probe(repo)
    assert g.status == "ok"
    assert g.dirty_count == 2
    assert isinstance(g.oldest_uncommitted_at, int)


def test_no_upstream_yields_none_not_crash(repo):
    """`git rev-list @{u}` exits 128 with 'no upstream configured'."""
    g = probe(repo)
    assert g.status == "ok"
    assert g.ahead is None
    assert g.behind is None


def test_detached_head_reports_literal_HEAD(repo):
    run(repo, "checkout", "-q", "--detach")
    g = probe(repo)
    assert g.status == "ok"
    assert g.branch == "HEAD"


def test_not_a_repo_is_a_first_class_state(tmp_path):
    """~43% of real project paths are not repos. This is not an error."""
    plain = tmp_path / "plain"
    plain.mkdir()
    g = probe(plain)
    assert g.status == "not_a_repo"
    assert g.dirty_count == 0
    assert g.branch is None


def test_missing_path_is_unavailable(tmp_path):
    assert probe(tmp_path / "nope").status == "unavailable"


def test_timeout_yields_unavailable(repo, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    monkeypatch.setattr(subprocess, "run", boom)
    assert probe(repo).status == "unavailable"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gitprobe.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bridge.gitprobe'`

- [x] **Step 3: Write the implementation**

`src/bridge/gitprobe.py`:
```python
"""Read-only git probe. Never mutates a repository.

Verified against the real environment:
  * `rev-list --left-right --count @{u}...HEAD` exits 128 with
    "no upstream configured" when no upstream exists -> ahead/behind stay None.
  * `rev-parse --abbrev-ref HEAD` returns the literal "HEAD" when detached,
    which matches `gitBranch: "HEAD"` values seen in real transcripts.
  * ~43% of tracked project paths are not repos at all, so `not_a_repo` is a
    normal outcome rather than a failure.
"""

import subprocess
from pathlib import Path

from bridge.models import GitState

GIT = "/usr/bin/git"


def _git(path: Path, *args: str, timeout: float) -> tuple[int, str]:
    proc = subprocess.run(
        [GIT, *args], cwd=path, capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, proc.stdout.strip()


def probe(path: Path, timeout: float = 2.0) -> GitState:
    path = Path(path)
    if not path.is_dir():
        return GitState(status="unavailable")
    try:
        code, out = _git(path, "rev-parse", "--is-inside-work-tree", timeout=timeout)
        if code != 0 or out != "true":
            return GitState(status="not_a_repo")

        g = GitState(status="ok")
        _, g.branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD", timeout=timeout)

        _, porcelain = _git(path, "status", "--porcelain", timeout=timeout)
        entries = [l for l in porcelain.splitlines() if l.strip()]
        g.dirty_count = len(entries)
        g.oldest_uncommitted_at = _oldest_mtime(path, entries)

        code, counts = _git(
            path, "rev-list", "--left-right", "--count", "@{u}...HEAD", timeout=timeout
        )
        if code == 0 and "\t" in counts:
            behind, ahead = counts.split("\t")[:2]
            g.behind, g.ahead = int(behind), int(ahead)

        code, last = _git(path, "log", "-1", "--format=%s%x09%ct", timeout=timeout)
        if code == 0 and "\t" in last:
            summary, ct = last.rsplit("\t", 1)
            g.last_commit_summary = summary
            g.last_commit_at = int(ct)
        return g
    except (subprocess.TimeoutExpired, OSError):
        return GitState(status="unavailable")


def _oldest_mtime(root: Path, porcelain_lines: list[str]) -> int | None:
    """Oldest mtime among changed files: how long work has sat uncommitted."""
    oldest: int | None = None
    for line in porcelain_lines:
        rel = line[3:].strip().strip('"')
        if " -> " in rel:  # rename
            rel = rel.split(" -> ", 1)[1]
        try:
            mt = int((root / rel).stat().st_mtime)
        except OSError:
            continue
        if oldest is None or mt < oldest:
            oldest = mt
    return oldest
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gitprobe.py -v`
Expected: 7 passed

- [x] **Step 5: Commit**

```bash
cd ~/dev/bridge
git add src/bridge/gitprobe.py tests/test_gitprobe.py
git commit -m "Add read-only git probe treating not-a-repo as a normal state"
```

---

### Task 6: Project registry

**Files:**
- Create: `src/bridge/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `bridge.registry.is_noise(dir_name: str) -> bool`
  - `bridge.registry.display_name(project_path: str) -> str`
  - `bridge.registry.transcript_files(projects_dir: Path) -> list[Path]`

The critical rule: transcript directory names encode `/` as `-`, which is **lossy**. `-Users-mitsheth-dev-Job-apps` could decode to `Job apps` (the real directory) or `Job-apps`. Real paths come only from the `cwd` field inside a transcript, never from decoding a directory name.

- [x] **Step 1: Write the failing tests**

`tests/test_registry.py`:
```python
from bridge.registry import display_name, is_noise, transcript_files


def test_hides_known_noise_directories():
    for name in [
        "-private-tmp-ecc-analysis",
        "-Users-mitsheth--claude",
        "-Users-mitsheth--local-share-ecc-homunculus",
        "-Users-mitsheth--local-share-ecc-homunculus-projects-047e75c52e2a",
        "-Volumes-mit-immich",
    ]:
        assert is_noise(name) is True, name


def test_keeps_real_projects():
    for name in [
        "-Users-mitsheth-dev-projectY",
        "-Users-mitsheth-dev-Job-apps",
        "-Users-mitsheth-dev-StreakSync",
    ]:
        assert is_noise(name) is False, name


def test_display_name_is_last_path_segment():
    assert display_name("/Users/mitsheth/dev/projectY") == "projectY"
    assert display_name("/Users/mitsheth/dev/Job apps") == "Job apps"
    assert display_name("/Users/mitsheth/dev/projectY/boardwatch") == "boardwatch"


def test_display_name_survives_trailing_slash():
    assert display_name("/Users/mitsheth/dev/demo/") == "demo"


def test_transcript_files_skips_noise_dirs(tmp_path):
    good = tmp_path / "-Users-mitsheth-dev-demo"
    good.mkdir()
    (good / "a.jsonl").write_text("")
    bad = tmp_path / "-private-tmp-ecc-analysis"
    bad.mkdir()
    (bad / "b.jsonl").write_text("")
    found = transcript_files(tmp_path)
    assert [p.name for p in found] == ["a.jsonl"]


def test_transcript_files_missing_dir_returns_empty(tmp_path):
    assert transcript_files(tmp_path / "nope") == []
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bridge.registry'`

- [x] **Step 3: Write the implementation**

`src/bridge/registry.py`:
```python
"""Which transcript directories are real projects, and what to call them.

Transcript directory names path-encode `/` as `-`, which is lossy:
`-Users-mitsheth-dev-Job-apps` maps to both "Job apps" (the real directory)
and "Job-apps". Real paths therefore come only from the `cwd` field inside a
transcript. Nothing here decodes a directory name into a path.
"""

from pathlib import Path

NOISE_PREFIXES = (
    "-private-tmp-",
    "-Users-mitsheth--claude",
    "-Users-mitsheth--local-share-ecc-homunculus",
    "-Volumes-",
)


def is_noise(dir_name: str) -> bool:
    return dir_name.startswith(NOISE_PREFIXES)


def display_name(project_path: str) -> str:
    return Path(project_path.rstrip("/")).name


def transcript_files(projects_dir: Path) -> list[Path]:
    projects_dir = Path(projects_dir)
    if not projects_dir.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(projects_dir.iterdir()):
        if not child.is_dir() or is_noise(child.name):
            continue
        out.extend(sorted(child.glob("*.jsonl")))
    return out
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_registry.py -v`
Expected: 6 passed

- [x] **Step 5: Commit**

```bash
cd ~/dev/bridge
git add src/bridge/registry.py tests/test_registry.py
git commit -m "Add project registry with cwd-based path resolution"
```

---

### Task 7: Indexer orchestration

**Files:**
- Create: `src/bridge/indexer.py`
- Test: `tests/test_indexer.py`

**Interfaces:**
- Consumes: `config.Config`, `store.Store`, `transcripts.scan`, `registry.transcript_files`, `registry.display_name`.
- Produces:
  - `bridge.indexer.IndexStats` dataclass: `files_seen: int`, `files_scanned: int`, `lines_parsed: int`, `parse_errors: int`, `sessions_upserted: int`.
  - `bridge.indexer.reindex(store: Store, cfg: Config, progress=None) -> IndexStats`

Skip rule: a file whose recorded `size` and `mtime` both match is not reopened at all. A file whose size **shrank** was rewritten and is re-scanned from offset 0.

- [x] **Step 1: Write the failing tests**

`tests/test_indexer.py`:
```python
import pytest

from bridge.config import load
from bridge.indexer import reindex
from bridge.store import Store
from tests.conftest import jline

SID = "22222222-2222-2222-2222-222222222222"


def transcript_lines(sid=SID, cwd="/Users/mitsheth/dev/demo", title="Did work"):
    return [
        jline(type="user", sessionId=sid, isSidechain=False,
              timestamp="2026-07-30T10:00:00.000Z", cwd=cwd, gitBranch="main",
              message={"role": "user", "content": "go"}),
        jline(type="assistant", sessionId=sid, isSidechain=False,
              timestamp="2026-07-30T10:05:00.000Z", cwd=cwd, effort="high",
              message={"role": "assistant", "model": "claude-opus-5",
                       "usage": {"input_tokens": 1, "output_tokens": 2}}),
        jline(type="ai-title", sessionId=sid, aiTitle=title),
    ]


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "projects"
    (projects / "-Users-mitsheth-dev-demo").mkdir(parents=True)
    cfg = load({"claude_projects_dir": projects, "db_path": tmp_path / "b.db"})
    store = Store(cfg.db_path)
    yield cfg, store, projects
    store.close()


def write(projects, name, lines, dirname="-Users-mitsheth-dev-demo"):
    p = projects / dirname / name
    p.write_text("".join(lines))
    return p


def test_first_index_creates_project_and_session(env):
    cfg, store, projects = env
    write(projects, "s.jsonl", transcript_lines())
    stats = reindex(store, cfg)
    assert stats.files_scanned == 1
    assert stats.sessions_upserted == 1
    projs = store.projects()
    assert len(projs) == 1
    assert projs[0]["path"] == "/Users/mitsheth/dev/demo"
    assert projs[0]["name"] == "demo"
    assert store.latest_session(projs[0]["id"])["title"] == "Did work"


def test_unchanged_file_is_not_rescanned(env):
    cfg, store, projects = env
    write(projects, "s.jsonl", transcript_lines())
    reindex(store, cfg)
    second = reindex(store, cfg)
    assert second.files_seen == 1
    assert second.files_scanned == 0
    assert second.lines_parsed == 0


def test_appended_file_scans_only_the_delta(env):
    cfg, store, projects = env
    p = write(projects, "s.jsonl", transcript_lines())
    reindex(store, cfg)
    with p.open("a") as f:
        f.write(jline(type="ai-title", sessionId=SID, aiTitle="Renamed"))
    stats = reindex(store, cfg)
    assert stats.files_scanned == 1
    assert stats.lines_parsed == 1
    pid = store.projects()[0]["id"]
    assert store.latest_session(pid)["title"] == "Renamed"


def test_shrunk_file_is_rescanned_from_zero(env):
    cfg, store, projects = env
    p = write(projects, "s.jsonl", transcript_lines())
    reindex(store, cfg)
    p.write_text("".join(transcript_lines(title="Rewritten")))
    stats = reindex(store, cfg)
    assert stats.files_scanned == 1
    pid = store.projects()[0]["id"]
    assert store.latest_session(pid)["title"] == "Rewritten"


def test_reindex_is_idempotent(env):
    cfg, store, projects = env
    write(projects, "s.jsonl", transcript_lines())
    reindex(store, cfg)
    reindex(store, cfg)
    reindex(store, cfg)
    pid = store.projects()[0]["id"]
    assert len(store.sessions(pid)) == 1


def test_record_without_cwd_is_skipped_not_fatal(env):
    cfg, store, projects = env
    write(projects, "nocwd.jsonl", [jline(type="assistant", sessionId="zz")])
    write(projects, "ok.jsonl", transcript_lines())
    stats = reindex(store, cfg)
    assert stats.sessions_upserted == 1  # only the one with a resolvable project
    assert len(store.projects()) == 1


def test_malformed_file_does_not_abort_the_run(env):
    cfg, store, projects = env
    write(projects, "bad.jsonl", ["{broken\n"])
    write(projects, "ok.jsonl", transcript_lines())
    stats = reindex(store, cfg)
    assert stats.parse_errors >= 1
    assert stats.sessions_upserted == 1


def test_two_projects_are_separated(env):
    cfg, store, projects = env
    (projects / "-Users-mitsheth-dev-other").mkdir()
    write(projects, "a.jsonl", transcript_lines())
    write(projects, "b.jsonl",
          transcript_lines(sid="33333333-3333-3333-3333-333333333333",
                           cwd="/Users/mitsheth/dev/other"),
          dirname="-Users-mitsheth-dev-other")
    reindex(store, cfg)
    assert {p["name"] for p in store.projects()} == {"demo", "other"}
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_indexer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bridge.indexer'`

- [x] **Step 3: Write the implementation**

`src/bridge/indexer.py`:
```python
"""Walk transcripts into the store, reading only what changed.

A file whose recorded size and mtime both match is never reopened. A file that
shrank was rewritten and is re-scanned from offset zero. One bad file never
aborts a run.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bridge.config import Config
from bridge.models import SessionRecord
from bridge.registry import display_name, transcript_files
from bridge.store import Store
from bridge.transcripts import scan


@dataclass
class IndexStats:
    files_seen: int = 0
    files_scanned: int = 0
    lines_parsed: int = 0
    parse_errors: int = 0
    sessions_upserted: int = 0


def reindex(
    store: Store, cfg: Config, progress: Callable[[int, int], None] | None = None
) -> IndexStats:
    stats = IndexStats()
    files = transcript_files(cfg.claude_projects_dir)
    total = len(files)

    for i, path in enumerate(files):
        stats.files_seen += 1
        if progress:
            progress(i + 1, total)
        try:
            _index_one(store, path, stats)
        except OSError:
            continue  # file vanished or unreadable mid-run; never fatal
    return stats


def _index_one(store: Store, path: Path, stats: IndexStats) -> None:
    st = path.stat()
    prior = store.get_scan_state(str(path))
    start, prev = 0, None

    if prior is not None:
        if prior["size"] == st.st_size and prior["mtime"] == st.st_mtime:
            return  # unchanged; do not open
        if st.st_size >= prior["size"]:
            start = prior["parsed_offset"]
            prev = _rehydrate(store, prior["session_id"], str(path))

    result = scan(path, start_offset=start, prev=prev)
    stats.files_scanned += 1
    stats.lines_parsed += result.lines_parsed
    stats.parse_errors += result.parse_errors

    rec = result.record
    sid = rec.session_id if rec else (prior["session_id"] if prior else None)
    store.set_scan_state(str(path), st.st_size, st.st_mtime, result.new_offset, sid)

    if rec is None or not rec.project_path:
        return  # no resolvable project; nothing to attribute the session to
    pid = store.upsert_project(rec.project_path, display_name(rec.project_path))
    store.upsert_session(rec, pid)
    stats.sessions_upserted += 1


def _rehydrate(store: Store, session_id: str | None, path: str) -> SessionRecord | None:
    """Rebuild the accumulator so an incremental scan adds onto prior totals."""
    if not session_id:
        return None
    row = store.conn.execute(
        "SELECT * FROM sessions WHERE id=?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    return SessionRecord(
        session_id=row["id"],
        transcript_path=path,
        project_path=None,
        title=row["title"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        model=row["model"],
        effort=row["effort"],
        git_branch=row["git_branch"],
        user_msgs=row["user_msgs"],
        assistant_msgs=row["assistant_msgs"],
        last_prompt=row["last_prompt"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        tokens_cache_create=row["tokens_cache_create"],
        tokens_cache_read=row["tokens_cache_read"],
        sidechain_tokens=row["sidechain_tokens"],
        interrupted=bool(row["interrupted"]),
    )
```

Note on `_rehydrate`: `project_path` is deliberately `None` so an incremental scan re-derives it from the appended `cwd` records. If the delta contains no `cwd`, the session keeps its existing project row and no re-attribution happens.

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_indexer.py -v`
Expected: 8 passed

- [x] **Step 5: Run the whole suite**

Run: `uv run pytest -v`
Expected: all tests from Tasks 1–7 pass (44 total)

- [x] **Step 6: Commit**

```bash
cd ~/dev/bridge
git add src/bridge/indexer.py tests/test_indexer.py
git commit -m "Add incremental indexer orchestration"
```

---

### Task 8: Card assembly

**Files:**
- Create: `src/bridge/cards.py`
- Test: `tests/test_cards.py`

**Interfaces:**
- Consumes: `store.Store`, `gitprobe.probe`, `models.Card`, `models.GitState`, `config.Config`.
- Produces:
  - `bridge.cards.build_cards(store: Store, cfg: Config, probe_fn=gitprobe.probe) -> list[Card]`
  - `bridge.cards.sort_key(card: Card) -> tuple` — actionability ordering.

Sort order (spec: "sorted by actionability, not alphabetically"). Phase 1 has no handoffs or live sessions yet, so the implemented order is: **stale-and-dirty → recently active → everything else**, each tie-broken by most recent session. Phase 2 inserts queued handoffs above stale, Phase 4 inserts running sessions above that. `sort_key` returns a tuple whose first element is a rank int, so later phases prepend ranks without restructuring.

- [x] **Step 1: Write the failing card tests**

`tests/test_cards.py`:
```python
import pytest

from bridge.cards import build_cards, sort_key
from bridge.config import load
from bridge.models import GitState, SessionRecord
from bridge.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "c.db")
    yield s
    s.close()


def add(store, path, name, sid, ended, tokens=10):
    pid = store.upsert_project(path, name)
    store.upsert_session(
        SessionRecord(session_id=sid, transcript_path=f"/t/{sid}",
                      project_path=path, title=f"work in {name}",
                      ended_at=ended, tokens_in=tokens, tokens_out=tokens),
        pid,
    )
    return pid


def test_card_carries_session_and_git(store, tmp_path):
    add(store, "/p/one", "one", "s1", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})
    cards = build_cards(store, cfg, probe_fn=lambda p: GitState(status="ok", branch="main"))
    assert len(cards) == 1
    assert cards[0].name == "one"
    assert cards[0].session.title == "work in one"
    assert cards[0].git.branch == "main"


def test_not_a_repo_is_never_stale(store, tmp_path):
    """~43% of real projects are not repos; they must not show the warning."""
    add(store, "/p/two", "two", "s2", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db", "stale_hours": 1})
    cards = build_cards(store, cfg, probe_fn=lambda p: GitState(status="not_a_repo"))
    assert cards[0].is_stale is False


def test_stale_when_uncommitted_older_than_threshold(store, tmp_path):
    add(store, "/p/three", "three", "s3", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db", "stale_hours": 12})
    old = GitState(status="ok", branch="main", dirty_count=47,
                   oldest_uncommitted_at=1)  # 1970
    assert build_cards(store, cfg, probe_fn=lambda p: old)[0].is_stale is True


def test_clean_repo_is_not_stale(store, tmp_path):
    add(store, "/p/four", "four", "s4", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})
    clean = GitState(status="ok", branch="main", dirty_count=0,
                     oldest_uncommitted_at=None)
    assert build_cards(store, cfg, probe_fn=lambda p: clean)[0].is_stale is False


def test_probe_failure_still_yields_a_card(store, tmp_path):
    """No probe failure may prevent rendering."""
    add(store, "/p/five", "five", "s5", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})

    def boom(_):
        raise RuntimeError("git exploded")

    cards = build_cards(store, cfg, probe_fn=boom)
    assert len(cards) == 1
    assert cards[0].git.status == "unavailable"


def test_stale_cards_sort_above_fresh(store, tmp_path):
    fresh = GitState(status="ok", branch="main")
    stale = GitState(status="ok", branch="main", dirty_count=9,
                     oldest_uncommitted_at=1)
    add(store, "/p/aaa", "aaa", "s6", "2026-07-30T10:00:00.000Z")
    add(store, "/p/zzz", "zzz", "s7", "2026-07-30T09:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})
    cards = build_cards(
        store, cfg, probe_fn=lambda p: stale if p == "/p/zzz" else fresh
    )
    assert cards[0].name == "zzz"  # stale wins over alphabetical and recency


def test_sort_key_rank_is_first_element(store, tmp_path):
    """Later phases prepend ranks; the contract is rank-first."""
    add(store, "/p/six", "six", "s8", "2026-07-30T10:00:00.000Z")
    cfg = load({"db_path": tmp_path / "c.db"})
    card = build_cards(store, cfg, probe_fn=lambda p: GitState(status="ok"))[0]
    assert isinstance(sort_key(card)[0], int)
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cards.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bridge.cards'`

- [x] **Step 3: Write the card implementation**

`src/bridge/cards.py`:
```python
"""Assemble one Card per project and order them by actionability.

Rank 0 is the most demanding of attention. Phase 2 will add queued handoffs and
Phase 4 running sessions above rank 0 by shifting these values; `sort_key`
returning a rank-first tuple is the contract that makes that a local change.
"""

from bridge import gitprobe
from bridge.config import Config
from bridge.models import Card, GitState, SessionRecord
from bridge.store import Store, now_epoch, to_epoch

RANK_STALE = 0
RANK_RECENT = 1
RANK_OTHER = 2

FIVE_HOURS = 5 * 3600
ONE_DAY = 24 * 3600


def build_cards(store: Store, cfg: Config, probe_fn=gitprobe.probe) -> list[Card]:
    now = now_epoch()
    cards: list[Card] = []

    for row in store.projects():
        try:
            git = probe_fn(row["path"])
        except Exception:  # noqa: BLE001 - a broken probe must not hide a card
            git = GitState(status="unavailable")

        cards.append(
            Card(
                project_id=row["id"],
                path=row["path"],
                name=row["name"],
                session=_session(store, row["id"]),
                git=git,
                tokens_today=store.token_totals(row["id"], now - ONE_DAY),
                tokens_5h=store.token_totals(row["id"], now - FIVE_HOURS),
                is_stale=_is_stale(git, cfg.stale_hours, now),
            )
        )

    cards.sort(key=sort_key)
    return cards


def _session(store: Store, project_id: int) -> SessionRecord | None:
    row = store.latest_session(project_id)
    if row is None:
        return None
    return SessionRecord(
        session_id=row["id"], transcript_path=row["transcript_path"] or "",
        title=row["title"], started_at=row["started_at"], ended_at=row["ended_at"],
        model=row["model"], effort=row["effort"], git_branch=row["git_branch"],
        user_msgs=row["user_msgs"], assistant_msgs=row["assistant_msgs"],
        last_prompt=row["last_prompt"], tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        tokens_cache_create=row["tokens_cache_create"],
        tokens_cache_read=row["tokens_cache_read"],
        sidechain_tokens=row["sidechain_tokens"],
        interrupted=bool(row["interrupted"]),
    )


def _is_stale(git: GitState, stale_hours: int, now: int) -> bool:
    """Only a real repo with real uncommitted work can be stale."""
    if git.status != "ok" or git.dirty_count == 0 or git.oldest_uncommitted_at is None:
        return False
    return (now - git.oldest_uncommitted_at) > stale_hours * 3600


def sort_key(card: Card) -> tuple:
    """Rank first, then most-recent-first, then name."""
    if card.is_stale:
        rank = RANK_STALE
    elif card.session is not None:
        rank = RANK_RECENT
    else:
        rank = RANK_OTHER
    ended = to_epoch(card.session.ended_at) if card.session else None
    return (rank, -(ended or 0), card.name.lower())
```

The `to_epoch` import comes from `bridge.store`; update the import line at the
top of the file to `from bridge.store import Store, now_epoch, to_epoch`.

- [x] **Step 4: Run card tests to verify they pass**

Run: `uv run pytest tests/test_cards.py -v`
Expected: 7 passed

- [x] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: all green. Nothing red is committed.

- [x] **Step 6: Commit**

```bash
cd ~/dev/bridge
git add src/bridge/cards.py tests/test_cards.py
git commit -m "Add card assembly with actionability ordering"
```

---

### Task 9: Read API and dashboard UI

**Files:**
- Create: `src/bridge/api.py`
- Create: `src/bridge/templates/base.html`, `src/bridge/templates/dashboard.html`, `src/bridge/templates/_card.html`, `src/bridge/templates/project.html`
- Create: `src/bridge/static/app.css`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `cards.build_cards`, `store.Store`, `config.Config`, `indexer.reindex`, `models.Card`.
- Produces: `bridge.api.create_app(store: Store, cfg: Config) -> fastapi.FastAPI`, plus Jinja filters `ago` (ISO string → `4m`/`3h`/`2d`), `ago_epoch` (epoch int → same), `kilo` (int → `5`/`12k`/`1.3M`).

API and templates ship together in one task because neither is testable without the other.

**REQUIRED:** invoke the `design-guardrails` skill before writing the CSS, per the standing repository rule for interface work.

Presentation rules from the spec, restated because they are testable requirements, not taste:
- Color carries meaning only: one accent for running (unused in Phase 1), one warning for stale. Nothing else colored.
- One number per concern, unit implied: `47 dirty`, not `Uncommitted changes: 47 files`.
- Status never by color alone — the `⚠` glyph and its `title` text carry it too.
- Token burn as absolute counts, never a percentage of a limit.
- Single column; two columns at ≥1400px.
- `not_a_repo` renders a neutral note, never a warning.

- [x] **Step 1: Write the failing API tests**

`tests/test_api.py`:
```python
import pytest
from fastapi.testclient import TestClient

from bridge.api import create_app
from bridge.config import load
from bridge.models import SessionRecord
from bridge.store import Store


@pytest.fixture
def client(tmp_path):
    cfg = load({"db_path": tmp_path / "a.db"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/demo", "demo")
    store.upsert_session(
        SessionRecord(session_id="s1", transcript_path="/t/s1.jsonl",
                      project_path="/Users/mitsheth/dev/demo",
                      title="Did the work", ended_at="2026-07-30T10:00:00.000Z",
                      model="claude-opus-5", effort="high", tokens_in=5,
                      tokens_out=5),
        pid,
    )
    app = create_app(store, cfg)
    yield TestClient(app), store, pid
    store.close()


def test_projects_endpoint_lists_projects(client):
    c, _, _ = client
    r = c.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["name"] == "demo"


def test_dashboard_renders_project_and_title(client):
    c, _, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "demo" in r.text
    assert "Did the work" in r.text


def test_dashboard_renders_with_zero_projects(tmp_path):
    cfg = load({"db_path": tmp_path / "empty.db"})
    store = Store(cfg.db_path)
    c = TestClient(create_app(store, cfg))
    r = c.get("/")
    assert r.status_code == 200
    store.close()


def test_project_detail_renders(client):
    c, _, pid = client
    r = c.get(f"/project/{pid}")
    assert r.status_code == 200
    assert "Did the work" in r.text


def test_unknown_project_returns_404(client):
    c, _, _ = client
    assert c.get("/project/99999").status_code == 404


def test_refresh_returns_stats(client):
    c, _, _ = client
    r = c.post("/api/refresh")
    assert r.status_code == 200
    assert "files_seen" in r.json()
```

- [x] **Step 2: Run API tests to verify they fail**

Run: `uv run pytest tests/test_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bridge.api'`

- [x] **Step 3: Write the API implementation**

`src/bridge/api.py`:
```python
"""FastAPI application. Read-only in Phase 1."""

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bridge.cards import build_cards
from bridge.config import Config
from bridge.indexer import reindex
from bridge.store import Store

HERE = Path(__file__).parent


def create_app(store: Store, cfg: Config) -> FastAPI:
    app = FastAPI(title="Bridge")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["ago"] = _ago
    templates.env.filters["ago_epoch"] = _ago_epoch
    templates.env.filters["kilo"] = _kilo
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        cards = build_cards(store, cfg)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "cards": cards,
                "totals": {
                    "today": sum(c.tokens_today for c in cards),
                    "last_5h": sum(c.tokens_5h for c in cards),
                    "projects": len(cards),
                },
            },
        )

    @app.get("/project/{project_id}", response_class=HTMLResponse)
    def detail(request: Request, project_id: int):
        row = store.conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown project")
        return templates.TemplateResponse(
            request,
            "project.html",
            {"project": row, "sessions": store.sessions(project_id)},
        )

    @app.get("/api/projects")
    def projects():
        return [dict(r) for r in store.projects()]

    @app.post("/api/refresh")
    def refresh():
        return asdict(reindex(store, cfg))

    return app


def _ago(iso: str | None) -> str:
    """Compact relative time: 4m, 3h, 2d. Empty when unknown."""
    from bridge.store import now_epoch, to_epoch

    epoch = to_epoch(iso)
    if epoch is None:
        return ""
    secs = max(0, now_epoch() - epoch)
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _ago_epoch(epoch: int | None) -> str:
    """Same shape as `ago`, for the epoch ints GitState carries."""
    from bridge.store import now_epoch

    if not epoch:
        return ""
    secs = max(0, now_epoch() - int(epoch))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _kilo(n: int | None) -> str:
    """Token counts as absolute magnitudes; never a percentage of a limit."""
    n = n or 0
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1_000_000:.1f}M"
```

- [x] **Step 4: Add the UI assertions to `tests/test_api.py`**

```python
def test_stale_project_shows_warning_glyph_and_text(tmp_path):
    """Status must not be conveyed by color alone (WCAG 2.2 AA)."""
    from bridge.models import GitState

    import bridge.cards as cards_mod

    cfg = load({"db_path": tmp_path / "s.db", "stale_hours": 1})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/stalerepo", "stalerepo")
    store.upsert_session(
        SessionRecord(session_id="s9", transcript_path="/t/s9",
                      project_path="/Users/mitsheth/dev/stalerepo", title="Old work",
                      ended_at="2026-07-30T10:00:00.000Z"),
        pid,
    )
    orig = cards_mod.gitprobe.probe
    cards_mod.gitprobe.probe = lambda p: GitState(
        status="ok", branch="main", dirty_count=47, oldest_uncommitted_at=1
    )
    try:
        c = TestClient(create_app(store, cfg))
        text = c.get("/").text
        assert "47 dirty" in text
        assert "⚠" in text
        assert "uncommitted" in text.lower()
    finally:
        cards_mod.gitprobe.probe = orig
        store.close()


def test_not_a_repo_shows_neutral_note_not_warning(tmp_path):
    from bridge.models import GitState

    import bridge.cards as cards_mod

    cfg = load({"db_path": tmp_path / "n.db"})
    store = Store(cfg.db_path)
    pid = store.upsert_project("/Users/mitsheth/dev/plain", "plain")
    store.upsert_session(
        SessionRecord(session_id="s10", transcript_path="/t/s10",
                      project_path="/Users/mitsheth/dev/plain", title="Work",
                      ended_at="2026-07-30T10:00:00.000Z"),
        pid,
    )
    orig = cards_mod.gitprobe.probe
    cards_mod.gitprobe.probe = lambda p: GitState(status="not_a_repo")
    try:
        text = TestClient(create_app(store, cfg)).get("/").text
        assert "not a git repo" in text.lower()
        assert "⚠" not in text
    finally:
        cards_mod.gitprobe.probe = orig
        store.close()


def test_tokens_shown_as_absolute_not_percentage(client):
    c, _, _ = client
    text = c.get("/").text
    assert "% of" not in text  # no fabricated denominator
    assert "today" in text.lower()
```

- [x] **Step 5: Run to verify they fail**

Run: `uv run pytest tests/test_api.py -v`
Expected: FAIL — templates directory does not exist yet.

- [x] **Step 6: Invoke design-guardrails**

Invoke the `design-guardrails` skill for a dense read-only status dashboard, then apply its rule cards to the CSS in Step 5.

- [x] **Step 7: Write the templates**

`src/bridge/templates/base.html`:
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Bridge{% endblock %}</title>
  <link rel="stylesheet" href="/static/app.css">
</head>
<body>
  <header class="topbar">
    <h1>Bridge</h1>
    {% block topbar %}{% endblock %}
  </header>
  <main>{% block content %}{% endblock %}</main>
</body>
</html>
```

`src/bridge/templates/dashboard.html`:
```html
{% extends "base.html" %}
{% block topbar %}
<dl class="totals">
  <dt>projects</dt><dd>{{ totals.projects }}</dd>
  <dt>tokens today</dt><dd>{{ totals.today | kilo }}</dd>
  <dt>last 5h</dt><dd>{{ totals.last_5h | kilo }}</dd>
</dl>
{% endblock %}
{% block content %}
{% if not cards %}
  <p class="empty">No projects indexed yet. Run <code>uv run python -m bridge index</code>.</p>
{% else %}
  <ul class="cards">
    {% for card in cards %}<li>{% include "_card.html" %}</li>{% endfor %}
  </ul>
{% endif %}
{% endblock %}
```

`src/bridge/templates/_card.html`:
```html
<article class="card{% if card.is_stale %} card--risk{% endif %}">
  <header class="card__head">
    <h2><a href="/project/{{ card.project_id }}">{{ card.name }}</a></h2>
    {% if card.session and card.session.model %}
      <span class="card__meta">{{ card.session.model }}{% if card.session.effort %}/{{ card.session.effort }}{% endif %}</span>
    {% endif %}
  </header>
  <p class="card__path">{{ card.path }}</p>

  {% if card.session %}
    <div class="card__session">
      <h3>{{ card.session.title or "Untitled session" }}</h3>
      <time datetime="{{ card.session.ended_at }}" title="{{ card.session.ended_at }}">{{ card.session.ended_at | ago }} ago</time>
      {% if card.session.interrupted %}<span class="card__note">interrupted</span>{% endif %}
    </div>
  {% else %}
    <p class="card__note">No indexed sessions.</p>
  {% endif %}

  <p class="card__git">
    {% if card.git.status == "ok" %}
      <span>{{ card.git.branch }}</span>
      {% if card.git.dirty_count %}<span> · {{ card.git.dirty_count }} dirty</span>{% endif %}
      {% if card.git.ahead %}<span> · {{ card.git.ahead }} ahead</span>{% endif %}
      {% if card.is_stale %}
        <span class="risk" title="Uncommitted work is older than the staleness threshold">⚠ uncommitted for {{ card.git.oldest_uncommitted_at | ago_epoch }}</span>
      {% endif %}
    {% elif card.git.status == "not_a_repo" %}
      <span class="card__note">not a git repo</span>
    {% else %}
      <span class="card__note">git unavailable</span>
    {% endif %}
  </p>

  <p class="card__burn">
    <span>{{ card.tokens_today | kilo }} today</span>
    <span>{{ card.tokens_5h | kilo }} last 5h</span>
  </p>
</article>
```

`src/bridge/templates/project.html`:
```html
{% extends "base.html" %}
{% block title %}{{ project.name }} — Bridge{% endblock %}
{% block content %}
<h2>{{ project.name }}</h2>
<p class="card__path">{{ project.path }}</p>
<table class="sessions">
  <caption>Indexed sessions, most recent first</caption>
  <thead>
    <tr><th scope="col">Session</th><th scope="col">Ended</th><th scope="col">Model</th><th scope="col">Turns</th><th scope="col">Tokens</th></tr>
  </thead>
  <tbody>
    {% for s in sessions %}
    <tr>
      <td>{{ s["title"] or "Untitled" }}</td>
      <td><time datetime="{{ s['ended_at'] }}">{{ s["ended_at"] | ago }} ago</time></td>
      <td>{{ s["model"] or "—" }}{% if s["effort"] %}/{{ s["effort"] }}{% endif %}</td>
      <td>{{ s["user_msgs"] }}/{{ s["assistant_msgs"] }}</td>
      <td>{{ (s["tokens_in"] + s["tokens_out"]) | kilo }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
```

- [x] **Step 8: Write the stylesheet**

`src/bridge/static/app.css`:
```css
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #16191d; --muted: #5c636b;
  --line: #d8dce1; --card: #fbfcfd;
  --risk-fg: #8a3a00; --risk-bg: #fff4e8; --accent: #0b5fb0;
  --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171a; --fg: #e8eaed; --muted: #a2abb4;
    --line: #2c3238; --card: #1b1f24;
    --risk-fg: #ffcf9e; --risk-bg: #3a2508; --accent: #7fb6f0;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
}
a { color: var(--accent); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.topbar {
  display: flex; align-items: baseline; gap: 2rem; flex-wrap: wrap;
  padding: 1rem 1.5rem; border-bottom: 1px solid var(--line);
}
.topbar h1 { font-size: 1rem; letter-spacing: .08em; text-transform: uppercase; margin: 0; }
.totals { display: flex; gap: 1.5rem; margin: 0; }
.totals dt { color: var(--muted); font-size: .8rem; }
.totals dd { margin: 0 0 0 .35rem; font-variant-numeric: tabular-nums; }
.totals dt, .totals dd { display: inline; }

main { padding: 1.5rem; }
.cards { list-style: none; margin: 0; padding: 0; display: grid; gap: 1rem; }
@media (min-width: 1400px) { .cards { grid-template-columns: 1fr 1fr; } }

.card {
  border: 1px solid var(--line); border-left: 3px solid var(--line);
  border-radius: 6px; background: var(--card); padding: 1rem 1.15rem;
}
.card--risk { border-left-color: var(--risk-fg); }
.card__head { display: flex; justify-content: space-between; gap: 1rem; align-items: baseline; }
.card__head h2 { font-size: 1.05rem; margin: 0; }
.card__meta, .card__path, .card__note { color: var(--muted); font-size: .82rem; }
.card__path { font-family: var(--mono); margin: .15rem 0 .9rem; }
.card__session { margin-bottom: .9rem; }
.card__session h3 { font-size: .95rem; font-weight: 600; margin: 0 0 .2rem; }
.card__session time { color: var(--muted); font-size: .8rem; }
.card__git, .card__burn {
  margin: .35rem 0 0; font-size: .85rem; color: var(--muted);
  display: flex; gap: .75rem; flex-wrap: wrap;
  font-variant-numeric: tabular-nums;
}
.risk {
  color: var(--risk-fg); background: var(--risk-bg);
  padding: .05rem .4rem; border-radius: 3px; font-weight: 600;
}
.empty { color: var(--muted); }
.sessions { border-collapse: collapse; width: 100%; font-size: .88rem; }
.sessions caption { text-align: left; color: var(--muted); padding-bottom: .5rem; }
.sessions th, .sessions td {
  text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--line);
}
.sessions td { font-variant-numeric: tabular-nums; }
```

- [x] **Step 9: Run the API tests to verify they pass**

Run: `uv run pytest tests/test_api.py -v`
Expected: 9 passed

- [x] **Step 10: Run the whole suite**

Run: `uv run pytest`
Expected: all passing (~60 tests)

- [x] **Step 11: Commit**

```bash
cd ~/dev/bridge
git add src/bridge/templates src/bridge/static src/bridge/api.py tests/test_api.py
git commit -m "Add dashboard and project detail UI"
```

---

### Task 10: Entry point and first real index

**Files:**
- Create: `src/bridge/__main__.py`
- Create: `README.md`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `config.load`, `store.Store`, `indexer.reindex`, `api.create_app`.
- Produces: `bridge.__main__.main(argv: list[str] | None = None) -> int`. Subcommands `index` and `serve`.

- [x] **Step 1: Write the failing test**

`tests/test_main.py`:
```python
from bridge.__main__ import main


def test_index_subcommand_runs_and_reports(tmp_path, capsys):
    projects = tmp_path / "projects"
    (projects / "-Users-mitsheth-dev-demo").mkdir(parents=True)
    code = main(["index", "--projects-dir", str(projects),
                 "--db", str(tmp_path / "b.db")])
    assert code == 0
    assert "files_seen" in capsys.readouterr().out


def test_unknown_subcommand_is_an_error(tmp_path):
    assert main(["nonsense"]) == 2
```

- [x] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bridge.__main__'`

- [x] **Step 3: Write the implementation**

`src/bridge/__main__.py`:
```python
"""Entry point: `python -m bridge index` and `python -m bridge serve`."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from bridge.config import load
from bridge.indexer import reindex
from bridge.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge")
    sub = parser.add_subparsers(dest="cmd")
    for name in ("index", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--projects-dir")
        p.add_argument("--db")

    args = parser.parse_args(argv)
    if args.cmd not in ("index", "serve"):
        parser.print_usage(sys.stderr)
        return 2

    overrides: dict = {}
    if args.projects_dir:
        overrides["claude_projects_dir"] = Path(args.projects_dir)
    if args.db:
        overrides["db_path"] = Path(args.db)
    cfg = load(overrides)
    store = Store(cfg.db_path)

    if args.cmd == "index":
        def progress(done: int, total: int) -> None:
            if done % 250 == 0 or done == total:
                print(f"  {done}/{total} files", file=sys.stderr)

        stats = reindex(store, cfg, progress=progress)
        print(json.dumps(asdict(stats), indent=2))
        store.close()
        return 0

    import uvicorn

    from bridge.api import create_app

    uvicorn.run(create_app(store, cfg), host="127.0.0.1", port=cfg.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_main.py -v`
Expected: 2 passed

- [x] **Step 5: Run the full suite one more time**

Run: `uv run pytest`
Expected: all passing

- [x] **Step 6: Index the real corpus**

Run: `cd ~/dev/bridge && time uv run python -m bridge index`

This is the one-time full read of 3.5 GB across 9,229 files; expect a few minutes. Record the reported `files_seen`, `sessions_upserted`, and `parse_errors`.

Then prove the incremental path:

Run: `time uv run python -m bridge index`
Expected: `files_scanned` near 0, `lines_parsed` near 0, and **wall time under a second**. If the second run takes anywhere near the first, the offset skip logic is broken — fix it before continuing.

- [x] **Step 7: Look at it**

Run: `uv run python -m bridge serve` and open http://127.0.0.1:8787

Confirm by eye: real project names, real session titles, `not a git repo` on the ~10 non-repo projects (`projectY`, `projectX`, `portfolio-website`, `longterm`, …), and no `⚠` on any of them.

- [x] **Step 8: Write the README**

`README.md`:
```markdown
# Bridge

A local control panel for Claude Code projects. Phase 1: read-only dashboard.

## Setup

    uv sync --extra dev

## Index transcripts

    uv run python -m bridge index

First run reads every transcript under `~/.claude/projects` and takes a few
minutes. Later runs read only appended bytes and finish in well under a second.

## Serve

    uv run python -m bridge serve   # http://127.0.0.1:8787

## Test

    uv run pytest

## Scope

Bridge never writes to a project repository. Its only writes are its own SQLite
database under `~/.bridge/`. All git access is read-only. It binds to localhost
only and has no authentication.

Phases 2–4 (handoff capture, session launching, live updates) are planned in
`docs/superpowers/plans/`.
```

- [x] **Step 9: Commit**

```bash
cd ~/dev/bridge
git add src/bridge/__main__.py tests/test_main.py README.md
git commit -m "Add CLI entry point for indexing and serving"
```

---

## Self-Review

**1. Spec coverage.** Every Phase 1 spec requirement maps to a task:

| Spec requirement | Task |
|---|---|
| Stack, Python 3.13 via uv, dependency limits | 1 |
| `transcripts` unit, `SessionRecord`, tolerant parsing | 2 |
| Incremental offset scanning, perf shape | 3, 7 |
| `store` unit, WAL, additive migrations, concurrent writers | 4 |
| `gitprobe` unit, not-a-repo / no-upstream / detached / timeout | 5 |
| `registry` unit, noise filtering, cwd-based path resolution | 6 |
| Backfill (idempotent, resumable, no LLM calls) | 7, 10 (Step 6) |
| Card content: last session, git, burn | 8, 9 |
| Actionability sort | 8 |
| Presentation rules, WCAG 2.2 AA, dark mode, 12h staleness | 9 |
| Detail view | 9 |
| Localhost-only bind | 10 |
| No probe failure blocks rendering | 8 (`test_probe_failure_still_yields_a_card`) |

Deliberately deferred, with the phase that covers them: `agents` probe, SSE, sparklines, and the diagnostics view (Phase 4); handoffs, spool, `bridge` CLI (Phase 2); launcher (Phase 3). `Card.spark` exists as an empty default so Phase 4 fills it without a schema change.

**2. Placeholder scan.** No TBDs. Every code step has runnable code, correct as written — no step mandates writing a known defect for a later step to repair, and no step commits a red test.

**3. Type consistency.** Checked across tasks: `SessionRecord` field names identical in `models.py`, `transcripts._apply`, `store.upsert_session`, `indexer._rehydrate`, and `cards._session`. `GitState.status` uses exactly `ok` / `not_a_repo` / `unavailable` in `gitprobe`, `cards._is_stale`, and `_card.html`. `ScanResult.lines_parsed` is produced in Task 2 and asserted in Tasks 3 and 7. `Store.set_scan_state` takes `session_id: str | None`, matching the `None` that `indexer` can pass.

One inconsistency found and fixed during review: `Store.token_totals` sums `tokens_in + tokens_out` only, excluding cache tokens. That is intentional and now stated here so a later phase doesn't "fix" it — cache reads are not new work and inflating burn with them would make every card's number meaningless.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-31-bridge-phase1-read-only-panel.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.
