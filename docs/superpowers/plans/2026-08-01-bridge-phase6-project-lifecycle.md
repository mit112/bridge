# Bridge Phase 6 — Project Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** The set of cards stops being "projects that have a transcript" and becomes "projects that
exist." A `~/dev/*` git repo you have not opened in Claude yet still gets a card, and a project whose
directory has been deleted archives itself instead of lingering as a dead card.

**Architecture:** Both changes are `reindex`-time, and neither adds a probe. `backfill` gains a
`dev_git_repos(cfg)` reader; `reindex` calls it and `upsert_project`s each repo (opt-out discovery,
`ON CONFLICT DO NOTHING` so a prior status is never disturbed). Auto-archive is a second post-index
pass that stamps a new `projects.missing_archived_at` column the first time a path is seen gone, so a
later manual restore in the panel is never silently re-archived — the same seed-vs-override rule the
config `[archived]` seed already follows, one row over.

**Tech Stack:** Python 3.13, SQLite (`sqlite3`, WAL, additive `COLUMN_MIGRATIONS`), FastAPI/Jinja
(unchanged here), pytest, `tools/falsify.py` mutation harness.

## Global Constraints

- Discovery is **opt-out**, never opt-in: a `~/dev/*` git repo becomes an `active` card; the user
  hides what they don't want (spec:240-241). Decision locked 2026-08-01.
- Auto-archive is **seed-style / once**: archive on the first reindex a path is seen missing; a UI
  restore sticks even if the path is still gone. Never silently undo a user action. Decision locked
  2026-08-01. (spec:412)
- **Never delete, always archive.** History is kept (spec:243).
- No new probe on any path in this phase. Everything is `reindex`-time and disk-cheap.
- Additive schema only: append to `store.COLUMN_MIGRATIONS`; never rewrite a `CREATE TABLE` or rebuild
  a table. (`store.py:135-144`)
- `upsert_project` is `INSERT ... ON CONFLICT(path) DO NOTHING` (`store.py:213`) — re-discovering an
  existing repo cannot reset its status. Rely on this; do not add a `project_by_path is None` guard
  around discovery.
- `store.projects(include_hidden=True)` returns **all** rows (active + hidden + archived); the default
  filters to `status='active'` (`store.py:253-257`).
- Mutation discipline: every shipped behavior gets a caught mutation in `tools/mutations/`. Commit
  before falsifying. Run `/Users/mitsheth/.local/bin/uv run python tools/falsify.py --spec ...`.

**Spec:** `docs/superpowers/specs/2026-07-31-bridge-control-panel-design.md` — §5 `registry`
(lines 239-246, opt-out discovery of `~/dev/*` git repos), and the error-handling row at line 412
("Project path no longer exists → Auto-archive, keep history").

---

## File Structure

- `src/bridge/store.py` — add `missing_archived_at` to `COLUMN_MIGRATIONS["projects"]`; add
  `archive_missing(project_id, at)`. One responsibility: persistence + additive migration.
- `src/bridge/backfill.py` — add `dev_git_repos(cfg) -> list[Path]`. Sits beside the existing
  `project_roots(store, cfg)`, which already reads `cfg.dev_dir`.
- `src/bridge/indexer.py` — `reindex` gains two post-index passes: dev-repo discovery and
  auto-archive-missing. Ensure `from pathlib import Path` is imported.
- `tests/test_store.py`, `tests/test_indexer.py`, `tests/test_backfill.py` — behavioral tests.
- `tools/mutations/project-lifecycle.json` — mutation coverage (new spec file).

Task 1 (auto-archive) and Task 2 (discovery) are independent and can land in either order; they touch
the same two files but disjoint code. Task 1 first, because the migration it adds is the riskiest
single change and is worth its own reviewer gate.

---

### Task 1: Auto-archive a project whose path has vanished

**Files:**
- Modify: `src/bridge/store.py` — `COLUMN_MIGRATIONS` (near line 139), new method near
  `set_project_status` (line 236).
- Modify: `src/bridge/indexer.py` — inside `reindex`, after the `unseen_archived` apply loop
  (after line 68), before `_link_background_launches` (line 71).
- Test: `tests/test_store.py`, `tests/test_indexer.py`.

**Interfaces:**
- Consumes: `store.projects(include_hidden=True)` → rows with `id`, `path`, `status`,
  `missing_archived_at`; `store.get_project(id)`; `store.set_project_status(id, status)`;
  `bridge.store.now_epoch()`.
- Produces: `store.archive_missing(project_id: int, at: int) -> None` — sets `status='archived'`
  **and** `missing_archived_at=at` in one UPDATE.

- [ ] **Step 1: Add the additive column migration.**

In `src/bridge/store.py`, extend the map (do not rewrite the `projects` `CREATE TABLE`):

```python
COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "sessions": {"last_usage_request_id": "TEXT"},
    # Epoch of the first reindex that found this project's path gone, so a later
    # manual restore in the panel is never re-archived. NULL = never seen missing.
    "projects": {"missing_archived_at": "INTEGER"},
}
```

- [ ] **Step 2: Write the failing store test.**

In `tests/test_store.py`:

```python
def test_archive_missing_sets_status_and_stamps_when_we_acted(store):
    pid = store.upsert_project("/gone/for/good", "gone")
    store.archive_missing(pid, at=1_780_000_000)
    row = store.get_project(pid)
    assert row["status"] == "archived"
    assert row["missing_archived_at"] == 1_780_000_000
```

- [ ] **Step 3: Run it, verify it fails.**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_store.py -k archive_missing`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'archive_missing'`.

- [ ] **Step 4: Implement `archive_missing`.**

In `src/bridge/store.py`, next to `set_project_status`:

```python
def archive_missing(self, project_id: int, at: int) -> None:
    """Archive a project whose directory has vanished, stamping WHEN we acted.

    The stamp is the whole point: the auto-archive pass skips any project that
    already carries one, so a user who restores a still-missing project in the
    panel is not silently re-archived on the next index. This is the config
    seed-vs-override rule applied one row over.
    """
    with self._lock:
        self.conn.execute(
            "UPDATE projects SET status='archived', missing_archived_at=? WHERE id=?",
            (at, project_id),
        )
```

- [ ] **Step 5: Run it, verify it passes.**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_store.py -k archive_missing` → PASS.

- [ ] **Step 6: Write the failing reindex tests.**

In `tests/test_indexer.py` (the `env` fixture yields `cfg, store, projects`):

```python
def test_reindex_archives_a_project_whose_path_has_vanished(env, tmp_path):
    cfg, store, _ = env
    pid = store.upsert_project(str(tmp_path / "deleted-project"), "deleted-project")
    reindex(store, cfg)
    row = store.get_project(pid)
    assert row["status"] == "archived"
    assert row["missing_archived_at"] is not None


def test_reindex_leaves_a_project_whose_path_still_exists_alone(env, tmp_path):
    cfg, store, _ = env
    here = tmp_path / "still-here"
    here.mkdir()
    pid = store.upsert_project(str(here), "still-here")
    reindex(store, cfg)
    row = store.get_project(pid)
    assert row["status"] == "active"
    assert row["missing_archived_at"] is None


def test_a_restored_project_is_not_re_archived_even_though_it_is_still_gone(env, tmp_path):
    """The seed-vs-override rule: config seeds once, the panel overrides. A
    manual restore of a still-missing project must survive the next index."""
    cfg, store, _ = env
    pid = store.upsert_project(str(tmp_path / "deleted-project"), "deleted-project")
    reindex(store, cfg)                        # archives + stamps
    store.set_project_status(pid, "active")    # user restores in the panel
    reindex(store, cfg)                        # must NOT re-archive
    assert store.get_project(pid)["status"] == "active"
```

- [ ] **Step 7: Run them, verify all three fail.**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_indexer.py -k "vanished or still_exists or re_archived"`
Expected: the vanished/re_archived tests FAIL (status stays `active`); `still_exists` may already
pass (nothing archives it yet) — that is the guard test, kept green throughout.

- [ ] **Step 8: Implement the auto-archive pass in `reindex`.**

In `src/bridge/indexer.py`, after the `unseen_archived` apply loop (after line 68), before
`_link_background_launches`. Ensure `from pathlib import Path` is imported at the top.

```python
    # Auto-archive a project whose directory has vanished (spec:412) -- but only
    # the FIRST run we see it gone. `missing_archived_at` records that we acted,
    # so a later manual restore in the panel is not silently undone at the next
    # index. Iterates all rows (active + hidden + archived): a hidden project
    # that was deleted should leave the hidden drawer too, and stamping an
    # already-archived one is a harmless no-op that still protects a future restore.
    for project in store.projects(include_hidden=True):
        if project["missing_archived_at"] is not None:
            continue
        if not Path(project["path"]).exists():
            store.archive_missing(project["id"], now_epoch())
```

- [ ] **Step 9: Run the reindex tests, verify all pass.**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_indexer.py -k "vanished or still_exists or re_archived"` → PASS.

- [ ] **Step 10: Full suite.**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q` → all pass (baseline was 559 before this phase).

- [ ] **Step 11: Commit.**

```bash
git add src/bridge/store.py src/bridge/indexer.py tests/test_store.py tests/test_indexer.py
git commit -m "Auto-archive a project whose path has vanished, once"
```

---

### Task 2: Card a `~/dev/*` git repo that has no transcripts

**Files:**
- Modify: `src/bridge/backfill.py` — add `dev_git_repos(cfg)` beside `project_roots` (line 88).
- Modify: `src/bridge/indexer.py` — inside `reindex`, after the indexing loop (after line 58),
  before the `unseen_archived` apply loop (line 65), so a discovered repo that is also a config
  `[archived]` seed still gets archived this run.
- Test: `tests/test_backfill.py`, `tests/test_indexer.py`.

**Interfaces:**
- Consumes: `cfg.dev_dir: Path` (default `~/dev`); `store.upsert_project(path: str, name: str) -> int`.
- Produces: `backfill.dev_git_repos(cfg) -> list[Path]` — sorted direct children of `cfg.dev_dir`
  that are directories containing a `.git` entry.

- [ ] **Step 1: Write the failing backfill test.**

In `tests/test_backfill.py` (build a `cfg` and override `dev_dir` with `dataclasses.replace`, since
`Config` carries `dev_dir` as a field):

```python
import dataclasses
from bridge import backfill

def test_dev_git_repos_lists_only_git_repos_under_dev_dir(tmp_path, base_cfg):
    dev = tmp_path / "dev"
    (dev / "has-git" / ".git").mkdir(parents=True)
    (dev / "plain-dir").mkdir()
    (dev / "a-file.txt").parent.mkdir(exist_ok=True)
    (dev / "a-file.txt").write_text("x")
    cfg = dataclasses.replace(base_cfg, dev_dir=dev)
    assert backfill.dev_git_repos(cfg) == [dev / "has-git"]
```

> `base_cfg` is any already-built `Config` (e.g. from the module's existing fixture or
> `load({"db_path": tmp_path / "b.db", "spool_dir": tmp_path / "s"})`). If `test_backfill.py` has no
> such fixture yet, build the cfg inline with `load(...)` and `dataclasses.replace`.

- [ ] **Step 2: Run it, verify it fails.**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_backfill.py -k dev_git_repos`
Expected: FAIL — `AttributeError: module 'bridge.backfill' has no attribute 'dev_git_repos'`.

- [ ] **Step 3: Implement `dev_git_repos`.**

In `src/bridge/backfill.py`, beside `project_roots`:

```python
def dev_git_repos(cfg) -> list[Path]:
    """Direct children of `~/dev` that are git repos, transcripts or not.

    A repo you have not opened in Claude still deserves a card (spec:240-241);
    discovery is opt-out, so the user hides the ones they don't want. The
    `registry` noise list is deliberately NOT applied here: it targets
    transcript-encoded container dirs (`-private-tmp-*`), which never appear
    under `~/dev`.
    """
    if not cfg.dev_dir.is_dir():
        return []
    return sorted(
        p for p in cfg.dev_dir.iterdir() if p.is_dir() and (p / ".git").exists()
    )
```

- [ ] **Step 4: Run it, verify it passes.**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_backfill.py -k dev_git_repos` → PASS.

- [ ] **Step 5: Write the failing reindex discovery test.**

In `tests/test_indexer.py`:

```python
import dataclasses

def test_reindex_cards_a_dev_repo_that_has_no_transcripts(env, tmp_path):
    cfg, store, _ = env
    dev = tmp_path / "dev"
    (dev / "lonely-repo" / ".git").mkdir(parents=True)
    (dev / "not-a-repo").mkdir()
    reindex(store, dataclasses.replace(cfg, dev_dir=dev))
    paths = {r["path"] for r in store.projects()}
    assert str(dev / "lonely-repo") in paths, "a transcript-less git repo gets an active card"
    assert str(dev / "not-a-repo") not in paths, "a plain dir is not a project"
```

- [ ] **Step 6: Run it, verify it fails.**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_indexer.py -k cards_a_dev_repo`
Expected: FAIL — `lonely-repo` not in `paths`.

- [ ] **Step 7: Implement discovery in `reindex`.**

In `src/bridge/indexer.py`, after the indexing loop (after line 58) and before the `unseen_archived`
apply loop (line 65):

```python
    # Discover `~/dev/*` git repos with no transcripts yet, so a repo you have
    # not opened in Claude still gets a card (spec:240-241). Opt-out: hide it if
    # you don't want it. `upsert_project` is ON CONFLICT DO NOTHING, so this
    # never disturbs the status of a repo a prior run already rowed. Placed
    # before the archived-seed apply so a dev repo that is also a config
    # `[archived]` seed is created here and archived by that loop in the same run.
    for repo in backfill.dev_git_repos(cfg):
        store.upsert_project(str(repo), repo.name)
```

Add `from bridge import backfill` to the imports if not already present.

- [ ] **Step 8: Run it, verify it passes.**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_indexer.py -k cards_a_dev_repo` → PASS.

- [ ] **Step 9: Guard test — discovery must not un-archive.**

```python
def test_reindex_discovery_does_not_unarchive_a_hidden_dev_repo(env, tmp_path):
    cfg, store, _ = env
    dev = tmp_path / "dev"
    (dev / "muted-repo" / ".git").mkdir(parents=True)
    cfg2 = dataclasses.replace(cfg, dev_dir=dev)
    reindex(store, cfg2)                                   # creates the row
    pid = store.project_by_path(str(dev / "muted-repo"))["id"]
    store.set_project_status(pid, "hidden")               # user mutes it
    reindex(store, cfg2)                                  # must stay hidden
    assert store.get_project(pid)["status"] == "hidden"
```

Run: `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_indexer.py -k does_not_unarchive` → PASS
(passes on the `ON CONFLICT DO NOTHING` semantics, no code change needed — this locks the guarantee).

- [ ] **Step 10: Full suite.**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q` → all pass.

- [ ] **Step 11: Commit.**

```bash
git add src/bridge/backfill.py src/bridge/indexer.py tests/test_backfill.py tests/test_indexer.py
git commit -m "Card a ~/dev git repo that has no transcripts yet"
```

---

### Task 3: Mutation coverage

**Files:**
- Create: `tools/mutations/project-lifecycle.json`.

- [ ] **Step 1: Write the spec.** Anchors must match verbatim; run
  `pytest -q tests/test_mutation_specs.py` (in the ordinary suite) to confirm each `old` matches
  `expect_count` times before falsifying. Lengthen an anchor if it reports `matches 2x`.

```json
[
  {
    "name": "never auto-archive a vanished path, so a deleted project lingers as a dead card forever",
    "file": "src/bridge/indexer.py",
    "old": "        if not Path(project[\"path\"]).exists():\n            store.archive_missing(project[\"id\"], now_epoch())",
    "new": "        pass",
    "tests": ["tests/test_indexer.py::test_reindex_archives_a_project_whose_path_has_vanished"],
    "expect_count": 1
  },
  {
    "name": "archive every missing path every run, so restoring a still-gone project is undone at the next index",
    "file": "src/bridge/indexer.py",
    "old": "        if project[\"missing_archived_at\"] is not None:\n            continue",
    "new": "        if False:\n            continue",
    "tests": ["tests/test_indexer.py::test_a_restored_project_is_not_re_archived_even_though_it_is_still_gone"],
    "expect_count": 1
  },
  {
    "name": "archive projects whose path still exists, evicting live cards",
    "file": "src/bridge/store.py",
    "old": "            \"UPDATE projects SET status='archived', missing_archived_at=? WHERE id=?\",",
    "new": "            \"UPDATE projects SET missing_archived_at=? WHERE id=?\",",
    "tests": ["tests/test_store.py::test_archive_missing_sets_status_and_stamps_when_we_acted"],
    "expect_count": 1
  },
  {
    "name": "skip dev-repo discovery, so a transcript-less repo never gets a card",
    "file": "src/bridge/indexer.py",
    "old": "    for repo in backfill.dev_git_repos(cfg):\n        store.upsert_project(str(repo), repo.name)",
    "new": "    pass",
    "tests": ["tests/test_indexer.py::test_reindex_cards_a_dev_repo_that_has_no_transcripts"],
    "expect_count": 1
  },
  {
    "name": "treat every dev child as a repo, carding plain directories that are not git repos",
    "file": "src/bridge/backfill.py",
    "old": "        p for p in cfg.dev_dir.iterdir() if p.is_dir() and (p / \".git\").exists()",
    "new": "        p for p in cfg.dev_dir.iterdir() if p.is_dir()",
    "tests": ["tests/test_backfill.py::test_dev_git_repos_lists_only_git_repos_under_dev_dir"],
    "expect_count": 1
  }
]
```

- [ ] **Step 2: Validate anchors on the ordinary suite.**

Run: `/Users/mitsheth/.local/bin/uv run pytest -q tests/test_mutation_specs.py` → PASS.

- [ ] **Step 3: Commit (falsify requires a clean tree at HEAD).**

```bash
git add tools/mutations/project-lifecycle.json
git commit -m "Add mutation coverage for project lifecycle"
```

- [ ] **Step 4: Falsify.**

Run: `/Users/mitsheth/.local/bin/uv run python tools/falsify.py --spec tools/mutations/project-lifecycle.json`
Expected: `5/5 mutations caught`. If one SURVIVES, check the test for vacuity FIRST (see the
mutation-survivor discipline); do not invent a test around a genuinely-equivalent mutant.

---

## Decisions (locked 2026-08-01, do not relitigate)

1. **Opt-out discovery.** Every non-noise `~/dev/*` git repo is an `active` card; the user hides
   unwanted ones. Rejected: "hidden by default" (more clicks, defeats discovery) and "only with a
   handoff" (discovery effectively never happens). ~13 dev git repos exist, most already indexed, so
   net-new empty cards are few.
2. **Seed-style auto-archive.** Archive on the first run a path is seen gone; a restore sticks.
   Rejected: "live rule / re-archive every run", which silently undoes a restore of a still-missing
   project. Implemented with the new `missing_archived_at` stamp, mirroring the config seed-vs-override
   rule.
3. **`registry.is_noise` is not applied to `~/dev` discovery.** The noise list targets
   transcript-encoded container dirs (`-private-tmp-*`, the 11 homunculus dirs, immich), which never
   appear as `~/dev` children. Applying it there would be a confusing no-op.
4. **No new probe.** Both passes are `reindex`-time and disk-cheap (`Path.exists`, one `iterdir`).

## Out of scope

- Un-archiving a project when its path reappears. A returned directory can be restored by hand; auto
  un-archive would fight the seed-vs-override rule from the other side. Revisit only if asked.
- Re-classifying existing noise-hiding; that shipped earlier and is unchanged.
- Any template/CSS change. Discovered and archived projects flow through the existing dashboard
  filter (`status='active'`) and hidden `<details>` unchanged.

## Self-Review

- **Spec coverage.** §5 opt-out discovery of `~/dev/*` git repos → Task 2. Line 412 auto-archive of a
  vanished path → Task 1. "Never delete, always archive" → `archive_missing` sets `status='archived'`,
  never deletes; history rows are untouched. Noise auto-hide (spec:241-243) already shipped
  (`registry.is_noise`) — no task, by design (Decision 3).
- **Placeholder scan.** No TBD/TODO; every code and test step carries real content. The only
  non-literal is `base_cfg` in Task 2 Step 1, annotated inline with how to build it.
- **Type consistency.** `archive_missing(project_id, at)` defined in Task 1 Step 4 and called in Task 1
  Step 8 with `(project["id"], now_epoch())`. `dev_git_repos(cfg) -> list[Path]` defined in Task 2
  Step 3, called in Task 2 Step 7 as `for repo in backfill.dev_git_repos(cfg)` with `repo.name` /
  `str(repo)`. `missing_archived_at` column read in Task 1 Steps 6/8 and written by `archive_missing`.
- **Ordering.** Discovery (Task 2) is placed before the `unseen_archived` apply loop; auto-archive
  (Task 1) after it. A dev repo that is also a config `[archived]` seed is created then archived in one
  run; auto-archive never touches a just-discovered repo because its path exists.
