# Bridge — spool journal for scheduled runs

**Date:** 2026-08-02
**Status:** designed, awaiting implementation plan
**Closes:** the durability gap deferred in
`docs/superpowers/specs/2026-08-01-bridge-scheduled-sessions-design.md:51-61`

## The gap

Handoffs are journaled to `~/.bridge/spool/drained/`, so `rm ~/.bridge/bridge.db` followed by
`bridge index` recovers them. Scheduled runs are equally authored data — a user typed that prompt
and picked that time — but live in SQLite only. Deleting the database loses every pending schedule
with no trace and no warning.

The scheduled-sessions spec named this explicitly rather than letting it slip:

> **v1 stores scheduled runs in SQLite only, with no spool journal.** … a manual DB delete loses
> pending schedules while handoffs survive — the one place scheduled runs are less durable than
> handoffs, by choice. If long-horizon scheduling ever lands, add journaling following the
> `spool.py` pattern. This is a decision to accept, not an oversight.

This spec is that follow-through. It also makes `store.py:396-397` accurate again — that comment
currently claims handoffs are "the only authored data here, so the only data that a dropped
database genuinely loses," which this change falsifies.

## Scope

**In:** an append-only journal for `scheduled_runs` covering creations and terminal statuses, and a
guarded replay into an empty table.

**Out:**

- No outbox. `bridge handoff` spools because the panel may be down; a schedule can only be authored
  *through* the panel (`POST /api/schedule` — there is no schedule CLI), so there is no offline
  authoring path to catch. This halves the module: no `write`, no `pending`, no `drain`.
- No journaling of the `launching` transition. It is not terminal, and losing the database mid-launch
  is handled correctly by the replay rules below without it.
- No new config field. The directory derives from the existing `cfg.spool_dir`.
- No change to when or whether jobs fire in normal operation. This is a recovery path only.

## Why creations plus terminal statuses is sufficient

`scheduled_runs` has nine mutation sites. Journaling all of them means nine places to keep in sync.
Replay only needs to answer two questions: *what work is still owed*, and *what is already
finished*. That drops the two claim paths — `claim_one_due` and `claim_specific` — leaving seven
sites that write to the journal, because `launching` is transient: a database
lost while a job is in `launching` leaves a creation record with no terminal record, which rule 4
below resolves to `missed`. Safe, and it needs no journal write on the claim path.

## Architecture

New module `src/bridge/schedspool.py`, mirroring `spool.py` the way `sessionmeta.py` mirrors
`gitprobe.py`.

It imports `_atomic_write` and `_quarantine` from `bridge.spool` rather than redefining them, and
deliberately does **not** relocate or alias them inside `spool.py`.
`tools/mutations/task1-store-and-spool.json` anchors on occurrence counts in that file, and both
moving the functions and adding a public alias would change those counts and break
`tests/test_mutation_specs.py`. Importing a package-private name across sibling modules costs
nothing and keeps `spool.py` byte-identical.

Records live in `~/.bridge/spool/schedules/`, a sibling of `drained/` and `bad/`. A separate
directory rather than a shared one is deliberate: `spool._load` globs `drained/*.json` and parses
anything not ending in `.status.json` as a `Handoff`. Schedule records in that directory would be
quarantined as corrupt handoffs. The `STATUS_SUFFIX` comment at `spool.py:33-35` already flags this
exact hazard for two record types; a third would compound it.

### Public surface

| function | writes | called from |
|---|---|---|
| `journal(job, spool_dir) -> Path` | `schedules/<id>.json` | create, retry, edit |
| `journal_status(id, status, at, spool_dir) -> Path` | `schedules/<id>.<at>.status.json` | cancel, finish, reconcile, prune |
| `rebuild_if_empty(store, spool_dir, now) -> RebuildStats` | — | `bridge index` |

`RebuildStats` is a small dataclass in the shape of `spool.DrainStats`, counting `restored`,
`missed`, `skipped_pruned`, `bad` and a `skipped` flag for the non-empty-table case.

`journal` serializes `dataclasses.asdict(ScheduledRun)`. An edit re-journals under the same
`<id>.json`, overwriting — identical to how `PATCH /api/handoff/{id}` keeps the journal's text
current (`api.py:865-878`).

Replay needs no `resolve` callable, unlike the handoff path: `scheduled_runs.project_path` is raw
TEXT with no foreign key to `projects`, so `store.create_scheduled_run(job)` is self-sufficient.

## Replay semantics

Guarded on `store.count_scheduled_runs() == 0`, the direct analogue of `handoff_count() > 0` at
`spool.py:232`. The guard is the whole point: replaying onto a live table would resurrect finished
jobs on every `bridge index`.

Creations and status records are loaded separately. For each creation, in `(created_at, id)` order:

1. A `pruned` status record exists → **skip entirely; do not insert.**
2. Any other terminal status exists → insert with that status.
3. No terminal status and `scheduled_for > now` → insert as `pending`.
4. No terminal status and `scheduled_for <= now` → insert as `missed`.

Where a job has several status records, the one with the greatest `at` wins.

**Rule 1** is why `pruned` is journal-only vocabulary and never a database status. Retention already
judged that row disposable; inserting it and then marking it would put a row back into the table
that the retention policy had removed. Skipping keeps the journal authoritative about deletions
without teaching the schema a status no live code path produces.

**Rule 4** is the answer to "a pending job whose time passed while the database was gone." It never
fires. Firing retroactively would launch a Claude session at an unpredictable time for work the user
may have long forgotten scheduling — the same reasoning that made `bridge launch` refuse to spool
(`docs/superpowers/plans/2026-07-31-bridge-phase3-launcher.md:50`). `missed` is terminal and visible
in the panel, and recovery is an explicit user action.

## The `missed` status

Added to the vocabulary in `models.py:148-150`, which becomes:

```
pending -> launching -> {fired, failed, indeterminate, cancelled}
                 replay -> missed
```

`missed` is set by replay and by nothing else. No scheduler tick, route, or store transition
produces it.

It is terminal, so `prune_scheduled_runs` reaps it with no change —
`status NOT IN ('pending','launching')` already covers it.

`retry_terminal`'s guard gains `'missed'`, so its clause becomes
`orig.status IN ('failed','indeterminate','missed')`. Without this a missed job is a dead end: the
retry route rejects it and `run-now` requires `pending`, leaving the user to retype the schedule by
hand. One word reuses the entire existing retry path, including the `source_handoff_id` carry-across
that is the reason `retry_terminal` exists.

## Store changes

`prune_scheduled_runs` and `reconcile_launching` currently return `int`. Both are bulk operations
whose affected rows must be journaled, so both return the list of affected ids instead. Their
callers at `__main__.py:102` and `__main__.py:109-111` use the value only for a log line and take
`len(...)`.

`reconcile_launching` journals `indeterminate` per id. This is fidelity rather than safety: without
it, a row reconciled to `indeterminate` and later replayed would land in `missed` by rule 4. Both
are terminal and neither fires, so the system stays correct either way — but the journal is cheap
once the ids are already in hand for prune, and a faithful replay is worth having.

## Call sites

Journal-before-database-write at every site, matching the existing convention (`api.py:815`, `:865`,
`:885`, `launcher.py:594`).

| site | file | journal call |
|---|---|---|
| `POST /api/schedule` | `api.py:970-994` | `journal(job)` |
| `PATCH /api/schedule/{id}` | `api.py:1018-1027` | `journal(updated_job)` |
| `DELETE /api/schedule/{id}` | `api.py:1029-1033` | `journal_status(id, 'cancelled')` |
| `POST /api/schedule/{id}/retry` | `api.py:1042-1065` | `journal(new_row)` |
| `_fire_claimed_job` | `api.py:327-384` | `journal_status(id, outcome)` for `fired` / `failed` / `indeterminate` |
| boot reconcile | `__main__.py:102` | `journal_status(id, 'indeterminate')` per id |
| boot prune | `__main__.py:109-111` | `journal_status(id, 'pruned')` per id |
| `bridge index` | `__main__.py:65` | `schedspool.rebuild_if_empty(...)` |

Hooking at call sites rather than inside `store.py` keeps the store a pure database layer with no
filesystem dependency and no `spool_dir` threaded through every constructor and fixture. The
accepted cost is that a *future* mutation site added without a journal call is a silent regression;
the mutation spec below is the guard against that.

## Error handling

Per-site, following what each existing site already does rather than inventing a uniform policy:

- **Creation** (`POST`, retry): a journal failure is reported, not raised — the response carries
  `journaled: false`, as at `api.py:810-812`. A failed journal must never cost the user the
  schedule.
- **Edit** (`PATCH`): a journal failure propagates, as at `api.py:851-857`. The journal must never
  lag the database it exists to rebuild.
- **Terminal statuses** from `_fire_claimed_job`: demoted to a logged warning. A launched session is
  not undone by a filesystem error, matching `launcher.py:595-596`.
- **Boot reconcile and prune**: logged and swallowed. Neither may block `bridge serve` from starting.
- **Replay**: a record that will not parse is quarantined to `bad/` and the replay continues, as at
  `spool.py:246-248`. One corrupt file never costs the others.

## Testing

New `tests/test_schedspool.py`, following `test_spool.py`'s shape: a `store` fixture, a `spool_dir`
fixture that is a path rather than a created directory, and a job builder.

The load-bearing test mirrors `test_drained_files_are_retained_and_can_rebuild_the_table`. It
deletes the database the way `rm` does, including sidecars:

```python
db.unlink()
for suffix in ("-wal", "-shm"):
    Path(str(db) + suffix).unlink(missing_ok=True)
```

Then asserts, in one scenario covering all four replay rules: a future pending job returns as
`pending`, a fired job as `fired`, a past-due pending job as `missed`, and a pruned job **not at
all**.

Further cases: replay is skipped when the table is non-empty; the greatest `at` wins among several
status records for one job; an edit's re-journal is what replays, not the original text; a corrupt
record is quarantined and the rest still replay; `retry_terminal` accepts a `missed` row.

`tests/conftest.py`'s autouse `never_touch_the_real_bridge_dir` guard tuple at `conftest.py:76-77`
grows to cover `schedspool`'s `journal`, `journal_status` and `rebuild_if_empty`. Its own docstring
warns this omission is invisible until a test has already written to the real `~/.bridge`, which
makes it the easiest thing in this change to forget.

New mutation spec `tools/mutations/schedule-spool.json` covering:

- removing the empty-table guard → replay resurrects finished jobs
- the `pruned` skip → a pruned job comes back
- the `scheduled_for <= now` boundary → a past-due job replays as `pending` and fires
- dropping the creation journal call in `POST /api/schedule` → nothing to replay

Existing anchors in `tools/mutations/scheduled-runs.json` and `scheduled-retry.json` must be
re-verified against the edited `store.py` and `api.py` via `tests/test_mutation_specs.py`.

## Documentation

`store.py:396-397` is updated: handoffs are no longer the only journaled authored data.
`schedspool.py` carries a module docstring in `spool.py`'s idiom, stating the append-only property,
naming its own regression test, and recording why `pruned` is journal-only and why `missed` never
fires.
