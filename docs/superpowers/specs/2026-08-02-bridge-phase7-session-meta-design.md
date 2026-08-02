# Bridge Phase 7 — session-meta enrichment

**Status:** DRAFT, awaiting review. Written 2026-08-02.

## Goal

The per-project detail page (`project.html`, shipped in Phase 5) lists a project's
indexed sessions with title, ended-at, model, turns, and tokens. Bridge already
knows far more about each session but throws it away: `~/.claude/usage-data/session-meta/*.json`
(the `/insights` output) carries files changed, lines added/removed, git
commits/pushes, duration, tool errors, interruptions, and capability flags
(Task-agent / MCP / web). Phase 7 surfaces **what each session actually did**,
per row, on the existing sessions table.

This is *enrichment*, not a new view. No new route, no new page, no project-level
rollup.

## Locked constraints (do not relitigate)

1. **The transcript parse stays the sole token authority.** `session-meta`'s
   `input_tokens` / `output_tokens` are **never read**. The `SessionMeta`
   dataclass does not even carry them. Tokens on the page come only from the
   `sessions` table, exactly as today. This is the single most important rule of
   the phase — a second, disagreeing token number is the failure mode Phase 7
   exists to avoid.
2. **The source is optional, incomplete, and never blocking.** `session-meta/` is
   capped at 200 sessions, newest-first, globally across all projects. Any given
   session may have no meta file, and older sessions usually won't. A missing
   file, a malformed file, or a mismatched file is not an error — the row simply
   renders as it does today. Enrichment never raises, never blocks a render,
   never fails a request.
3. **Never the basis of a total.** Enrichment is per-row only. There is no
   aggregate across sessions, because a sum over an optional 200-capped source is
   a misleading total the moment meta is missing for older rows.

## Approach: read at request time, no persistence

The detail route reads meta opportunistically when it builds the page, keyed by
session id, straight off disk. Nothing is indexed into SQLite.

**Why not index it into the DB during the scan loop?** Persisting meta would let
enrichment survive a session aging out of the rolling 200-window, but it buys
that at the cost of a schema change, coupling to the indexer, and — worst —
a *second copy* of session facts living in the DB that can drift from the source.
The spec's own words for this source are "read opportunistically when a file for
a session ID exists; never required, never blocking." That is request-time read
semantics. The cost of reading is trivial: a detail page shows at most
`sessions(limit=50)` rows, so at most 50 direct `open()`s of ~1 KB files keyed by
exact filename (`{session_id}.json`) — no directory scan. The rolling-window gap
(a session loses its enrichment once it ages past the newest 200) is acceptable
and is exactly the "optional, incomplete" contract.

## Component: `sessionmeta.py`

A new flat module beside `gitprobe.py` / `transcripts.py`, following their shape:
pure functions over paths, stdlib only, tolerant of absent keys.

```python
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
    uses_web: bool          # session-meta's web_search OR web_fetch, collapsed
    # input_tokens / output_tokens are DELIBERATELY absent (constraint 1).

    @property
    def has_signal(self) -> bool:
        # True iff any surfaced fact is nonzero/true. A meta file that exists but
        # records a pure Q&A session (all zeros, like a 0-minute eval) has no
        # signal and renders as an empty enrichment, same as no file at all.

def read(session_id: str, meta_dir: Path = DEFAULT_META_DIR) -> SessionMeta | None:
    # Construct the path from the id, parse, and:
    #   - OSError (missing/unreadable)            -> None
    #   - JSON ValueError (partial/corrupt write) -> None
    #   - raw["session_id"] != session_id         -> None  (defensive; the file
    #        is named for its id, so a mismatch means a renamed/corrupt file)
    # Every int/bool field is read with a tolerant default (absent key -> 0/False),
    # because the CLI that wrote these varies across months of corpus.

def read_many(session_ids, meta_dir=DEFAULT_META_DIR) -> dict[str, SessionMeta]:
    # {id: SessionMeta} for the ids that have a usable, signal-bearing file.
    # Ids with no file / no signal are simply absent from the map.
```

`meta_dir` follows the existing `Config` convention: add a
`session_meta_dir = home / ".claude" / "usage-data" / "session-meta"` field
beside `claude_projects_dir` (config.py:197), and the detail route passes
`cfg.session_meta_dir` through. Tests build a `Config` pointing at a fixture
directory, exactly as they do for `claude_projects_dir` today. `read_many` omits
both missing-file and no-signal ids, so the template only has to check presence.

## Route + template changes

**`api.py` `detail()`**: after fetching `sessions`, build
`metas = sessionmeta.read_many([s["id"] for s in sessions], meta_dir)` and pass
`metas` into the template context. One added call; the existing token/turn
rendering is untouched.

**`project.html` sessions table**: add one **"Changes"** column. For each row,
`{% set m = metas.get(s["id"]) %}`; when `m` is present, render only the facts
that carry signal (mirroring how the Phase 5 tokens cell shows cache/sidechain
only when nonzero):

- Primary line: `{files_modified} files · +{lines_added}/−{lines_removed}`
  (each part omitted when zero).
- Detail sub-line (`sessions__meta-detail` span, styled like
  `sessions__tokens-detail`): `{git_commits} commits · {git_pushes} pushes ·
  {duration_minutes}m`, then friction (`{tool_errors} tool errors`,
  `{user_interruptions} interruptions`) and capability badges
  (`agent` / `mcp` / `web`) — every item rendered only when nonzero/true.

When `m` is absent the cell is empty. No row ever changes height or layout based
on whether Bridge happened to have a meta file — the column is always present,
just sometimes empty.

Exact spacing, badge styling, and the sub-line's visual weight are an
implementation-time concern and MUST pass the `design-guardrails` skill (WCAG 2.2
AA, no icon-only meaning, sufficient contrast on the badges) before the phase is
called done.

## Edge cases

| Case | Behavior |
|---|---|
| No meta file for the session | Row renders exactly as today; id absent from `metas`. |
| Malformed / partially written JSON | `read` returns `None`; treated as missing. Never raises. |
| File's `session_id` ≠ requested id | Treated as missing (defensive against a renamed/corrupt file). |
| Meta exists, all facts zero (pure Q&A) | `has_signal` false → omitted from `metas` → empty cell. |
| Absent individual keys | Tolerant defaults (0 / False) per field. |
| `meta_dir` doesn't exist at all | Every `read` is an `OSError` → every row empty. No crash. |

## Testing

- **`tests/test_sessionmeta.py`** (unit, the bulk): valid file → populated
  dataclass; missing file → `None`; malformed JSON → `None`; mismatched
  `session_id` → `None`; all-zero file → `None` via `has_signal`; absent keys →
  defaults; `uses_web` true when either web flag set; `read_many` filters
  correctly.
- **`tests/test_api.py`**: detail page renders the Changes cell when a fixture
  meta is present; renders the page unchanged (no cell content, no 500) when the
  meta dir is empty or a file is malformed.
- **`tests/test_static_js.py` / template test**: the guarded facts appear only
  when nonzero (no `0 files`, no stray `·` separators).
- **Mutation spec `tools/mutations/session-meta.json`**: pin the guards — the
  `session_id` mismatch check, the `has_signal` gate, the per-fact nonzero
  render conditions, and the token-absence (a mutation that tries to read
  `input_tokens` must be caught by a test asserting the page's token number comes
  only from the transcript). Follow the mutation-survivor discipline: a survivor
  is a vacuous test until proven otherwise.

## Out of scope (deferred, unchanged)

- Persisting meta into SQLite to survive the 200-window rolling cap.
- Project-level or cross-session aggregates of any meta field.
- A dedicated per-session drill-down page (tool_counts breakdown, languages,
  response-time distribution, hours-of-day).
- Surfacing `tool_counts` / `languages` maps — the phase ships scalar facts only;
  the maps are richer UI and belong with a future drill-down.
