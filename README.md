# Bridge

A local control panel for Claude Code projects: see each project's state, keep the
next-session prompt with it, and launch the session from the card.

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

Phases 1–3 (read-only dashboard, handoff capture, session launching) are merged.
Phase 4 (live updates: an `agents` probe, cached git state, sparklines, diagnostics
and SSE) is planned but not started. Every phase's plan is in
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

## Launching a session

Press ▶ on a card to open the queued prompt as a real session in a new Terminal window,
with the model and effort chosen beside the button. Edits to the prompt are saved as you
make them, and the exact bytes each launch ran are recorded separately, so a later edit
cannot rewrite what actually ran. The same thing from the shell:

    bridge launch [--project P] [--mode terminal|background] [--model M] [--effort E]

Unlike `bridge handoff`, this exits non-zero when the panel is down and does **not** spool.
Nothing is lost — you can run `claude` yourself — and a launch that fires at an
unpredictable later time is worse than one that never fires.

In terminal mode the prompt never reaches the command line: it is written to
`~/.bridge/launches/<session-id>.prompt` and read back by the new shell. A terminal launch
pre-assigns its session UUID, so the next `bridge index` joins it to its own transcript;
`claude --bg` mints its own id, so background launches correlate afterwards instead.
