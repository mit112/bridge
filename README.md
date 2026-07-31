# Bridge

A local control panel for Claude Code projects. Phase 1: read-only dashboard.

## Setup

    uv sync --extra dev

## Index transcripts

    uv run python -m bridge index

First run reads every transcript under `~/.claude/projects` and takes
approximately 15 seconds. Later runs read only appended bytes and finish
in well under a second.

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

## The handoff loop

End a session by running `/handoff`. It composes a summary and a next-session prompt and
records them against the current project; `bridge next` prints the prompt back, so
`claude "$(bridge next)"` opens the next session on it.

Install the slash command once (Bridge never writes outside `~/.bridge`, so this is
deliberately manual):

```bash
cp ~/dev/bridge/commands/handoff.md ~/.claude/commands/handoff.md
```

The panel is started by hand with `bridge serve`, so it is usually down. That is fine:
`bridge handoff` exits zero regardless, writes the prompt to `~/.bridge/spool/`, and the
server ingests it on the next boot. Drained spool files are retained in
`~/.bridge/spool/drained/` as an append-only journal, which is what keeps
`rm ~/.bridge/bridge.db && bridge index` a safe operation now that Bridge stores authored
data.

`bridge backfill` imports stray `HANDOFF.md` / `NEXT-SESSION.md` files. It is `--dry-run`
unless you pass `--write`.
