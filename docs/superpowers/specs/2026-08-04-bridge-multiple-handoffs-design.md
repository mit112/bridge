# Multiple concurrent handoffs per project

**Date:** 2026-08-04
**Status:** Design approved, pending spec review

## Problem

A project today can hold exactly one *queued* handoff. Authoring a new one
supersedes any existing queued handoff for that project (`store.create_handoff`,
`store.py:400`). That models one line of work per project.

But a project routinely hosts more than one line of work at a time. Concrete
case that motivated this: a planning session ends and queues its handoff, while
a *separate* session working on the Bridge UI ends and queues its own. Today the
second wipes the first. The user wants **both queued handoffs visible on the
panel at once, and to choose which to fire, when.**

## Goal

Let several handoffs stay queued for one project simultaneously, each shown as
its own fireable item, without the user having to name, group, or organize
anything. It should "just work": two sessions → two handoffs → both waiting.

## The one behavioral change

**Scope supersession to the authoring session instead of the whole project.**

- Today: a new handoff sets *every* queued handoff in the project to
  `superseded`, then inserts.
- New: a new handoff supersedes only queued handoffs **from the same
  `source_session_id`**, then inserts.

The distinguishing key already exists — every handoff records which Claude
session wrote it (`Handoff.source_session_id`, POSTed by the `/handoff` skill as
`$CLAUDE_CODE_SESSION_ID`). Two different sessions carry different ids, so their
handoffs coexist. Re-running `/handoff` inside the *same* session still replaces
that session's own handoff, so revising a handoff mid-session does not spam
duplicates.

**New invariant:** at most one queued handoff per `(project_id,
source_session_id)`; N such pairs per project.

### Edge: null `source_session_id`

`source_session_id` is nullable (an ad-hoc `bridge handoff` with no session).
Two handoffs both with `NULL` source would collide under the pair key. Rule:
**when `source_session_id IS NULL`, never supersede** — each such handoff stands
alone. This is the rare path (the skill always passes a session id); treating
each anonymous handoff as its own item is the safe, non-destructive choice.

## What is explicitly NOT in scope

- **No `threads` table, no named workstreams.** Grouping is implicit via
  session id; the user names nothing. (A named-thread model was considered and
  rejected as unnecessary ceremony for the stated need.)
- **No ordering / backlog / drain semantics.** Handoffs are a *set* the user
  picks from, not a queue drained in order.
- **No cross-session merge or dependency between handoffs.**

## Changes by layer

### Store (`store.py`)

- `create_handoff`: change the supersede statement from
  `WHERE project_id=? AND status='queued' AND id<>?`
  to additionally scope by session:
  `WHERE project_id=? AND status='queued' AND source_session_id=? AND id<>?`,
  and **skip the supersede entirely when `source_session_id IS NULL`**. The
  insert and its `ON CONFLICT(id) DO NOTHING` idempotency are unchanged, so
  spool re-drain stays harmless.
- Replace `queued_handoff(project_id) -> Row | None` with
  `queued_handoffs(project_id) -> list[Row]` (all queued for the project,
  `ORDER BY created_at DESC`). Keep a thin `queued_handoff` returning the first
  of that list only if an existing caller genuinely needs "the newest one"
  (audit call sites first; prefer removing the singular).
- `queued_handoff_count` (project-wide diagnostics) is unchanged — it already
  counts all queued rows.

No schema change. No migration. Existing rows already carry `source_session_id`;
every current single-handoff project keeps behaving identically until a second
session queues one.

### API (`api.py`)

- `GET /api/handoff?project_path=` and `GET /api/handoff/{project_id}` return a
  **list** of queued handoffs instead of one (or gain a plural sibling; pick one
  and update the client). `POST /api/handoff` is unchanged on the wire — the
  server-side scoping change in the store does the work.
- `POST /api/launch` already accepts an explicit `handoff_id`; per-handoff Run
  passes it. **Remove or make deterministic the implicit fallback** that grabs
  "the project's queued handoff" when no `handoff_id` is given — with several
  queued it is ambiguous. Preferred: launching a handoff always names its id;
  the no-id path launches the ad-hoc compose prompt only.

### Cards (`cards.py`, `models.py`)

- `Card.handoff: dict | None` becomes `Card.handoffs: list[dict]` (empty list
  when none). Update the `models.py:194` comment that asserts "at most one."
- `build_cards` populates the list from `queued_handoffs`.

### Templates (`_launch.html`, dashboard/detail)

- The `handoff_block` / `handoff_actions` macros key off a single
  `card.handoff`. Wrap them so the card **stacks every queued handoff inline** —
  one block per handoff, each with its own summary label, "queued N ago", Run,
  and dismiss. The macros stay per-handoff; the caller loops
  `for h in card.handoffs`.
- Each block's summary line is the item's identity (the user already writes a
  specific one-liner per handoff). No new label field.

### Static JS

- Any handoff-selection / prompt-edit / dismiss wiring keyed on a single
  handoff per card must key on the specific handoff id within the loop. The
  per-handoff `data-handoff-section` / `data-prompt-handoff` hooks already carry
  the id, so this is mostly ensuring nothing assumes one-per-card.

## Testing

TDD, `uv run pytest` (currently ~1025 passing). New/changed coverage:

1. **Store:** two handoffs with different `source_session_id` on the same
   project → both `queued`; `queued_handoffs` returns both.
2. **Store:** re-handoff with the *same* `source_session_id` → old one
   `superseded`, only the new one queued (revision, not duplication).
3. **Store:** two handoffs both `source_session_id IS NULL` → both remain
   queued (no supersede).
4. **Store:** spool re-drain of an already-queued handoff is still idempotent
   and does not supersede its siblings.
5. **API:** `GET` returns the full queued list; `POST` of a second session's
   handoff does not disturb the first.
6. **Launch:** `POST /api/launch` with an explicit `handoff_id` fires exactly
   that handoff and leaves the others queued.
7. **Cards/template:** a card with two queued handoffs renders two stacked
   blocks, each with its own Run.
8. **Regression:** a project with one queued handoff renders and fires exactly
   as before.

## Rollout

Pure additive behavior on existing data — no migration, no backfill. Ship
behind normal test gate. First observable moment is when a second session
queues a handoff on a project that already has one.
