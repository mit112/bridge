---
description: Capture a next-session prompt for this project in Bridge
---

Record a handoff for the next session in this project. Compose it first, then hand it to
the `bridge` CLI on stdin.

## 1. Compose

From the session so far, write two things:

**A summary** — one line, past tense, specific. What this session actually changed.
Not "worked on the CLI" but "added the bridge CLI with spool-on-failure; 7/7 mutations caught".

**A next-session prompt** — addressed to the next Claude, who will start with no memory of
this conversation. A good one states: the repo and branch, what is done and committed, what
is next and why, any decision already taken that should not be relitigated, and any trap
worth knowing. Write it as an instruction, not a status report: it becomes that session's
opening message.

Be specific about state that is expensive to rediscover — exact commands, file paths,
commit hashes, measured numbers.

## 2. Record

Run exactly this, substituting your summary and prompt:

```bash
summary=$(cat <<'BRIDGE_SUMMARY'
<your one-line summary>
BRIDGE_SUMMARY
)
bridge handoff \
  --summary "$summary" \
  --session-id "$CLAUDE_CODE_SESSION_ID" \
  --effort "$CLAUDE_EFFORT" \
  --prompt-file - <<'BRIDGE_PROMPT'
<your next-session prompt, as many paragraphs as it needs>
BRIDGE_PROMPT
```

Rules that matter:

- **Both heredoc delimiters are quoted** (`<<'BRIDGE_SUMMARY'`, `<<'BRIDGE_PROMPT'`). That
  disables every form of shell expansion, so text containing `$(...)`, backticks, `${VAR}`,
  quotes or backslashes is transmitted literally. An unquoted delimiter would execute part
  of what you wrote.
- **The summary goes through a heredoc too, not straight into the command line.** Writing
  `--summary "<text>"` looks harmless and is not: a summary containing a double quote gets
  silently mangled, and one containing `$(...)` or backticks gets *executed*. Assigning it
  to a variable first and passing `"$summary"` is safe, because a quoted variable expansion
  is not re-evaluated.
- **Never pass the prompt as an argument.** It goes on stdin via `--prompt-file -`, always.
  Prompts contain newlines and quotes and are routinely tens of kilobytes.
- **Do not `cd` first.** `--project` defaults to the current directory, and the server
  resolves it — including through the alias table, so an old `~/Documents/...` path still
  attaches to the right project.
- Pick a delimiter that does not appear in your prompt. If the prompt might contain
  `BRIDGE_PROMPT`, use another one.

## 3. Interpret the exit status

- **Exit 0, stderr says `queued for <path>`** — recorded in the panel. Done.
- **Exit 0, stderr says `spooled to <path>`** — also success. The panel is not running,
  which is the normal case; the handoff is on disk and the server ingests it the next time
  it starts. Report it as captured, not as a failure.
- **Exit 0, stderr prints the prompt back** — spooling itself failed. The prompt survives in
  this transcript only. Tell the user plainly and paste the path you tried.
- **Non-zero** — a real failure. The usual cause is an empty prompt, which exits 2. Fix and
  rerun; nothing was recorded.

## 4. Confirm

Tell the user in one line what was captured and where it went. If the panel is running, they
can see it at http://127.0.0.1:8787; otherwise mention it will appear on next `bridge serve`.

## Installation note

This file lives in the Bridge repo at `commands/handoff.md` and must be copied to
`~/.claude/commands/handoff.md` to be usable. Bridge never writes outside `~/.bridge`, so
that copy is a deliberate manual step:

```bash
cp ~/dev/bridge/commands/handoff.md ~/.claude/commands/handoff.md
```
