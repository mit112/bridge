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
