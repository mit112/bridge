---
description: Capture a next-session prompt for this project in Bridge
---

Record a handoff for the next session in this project. Compose it first, then hand it to
the `bridge` CLI on stdin.

## 0. Probe

Compose from what the repo says, not from what you remember. At the end of a long session
the commit you think you made and the branch you think you are on are both guesses, and a
handoff that states them wrongly sends the next session somewhere that does not exist.

Run this first and keep the output in front of you:

```bash
git rev-parse --abbrev-ref HEAD
git rev-parse --short HEAD
git status --porcelain | wc -l
git rev-list --left-right --count '@{u}...HEAD' 2>/dev/null || echo "no upstream"
git log --oneline -5
```

`@{u}...HEAD` prints `<behind>` then `<ahead>`, in that order. A non-zero ahead count means
the work is committed but **not pushed** — say so in as many words. "Done" and "pushed" are
different claims, and the next session will act on the difference.

Then add whatever else your claims rest on: the repo's named gate and its real exit status
(`make check`, `.claude/verify.sh` — run it, do not recall it), the test count, the measured
number you are about to quote. Anything you cannot re-derive right now, write down as an
uncertainty rather than as a fact.

## 1. Compose

From the session so far, write two things:

**A summary** — one line, past tense, specific. What this session actually changed.
Not "worked on the CLI" but "added the bridge CLI with spool-on-failure; 7/7 mutations caught".

**A next-session prompt** — addressed to the next Claude, who will start with no memory of
this conversation. A good one states: the repo and branch, what is done and whether it is
committed *and* pushed, what is next and why, what was deliberately **not** done and why
not, any decision already taken that should not be relitigated, and any trap worth knowing.
Write it as an instruction, not a status report: it becomes that session's opening message.

Be specific about state that is expensive to rediscover — exact commands, file paths,
commit hashes, measured numbers. Take them from the probe above, not from memory.

**"Deliberately not done" earns its place.** A prompt that is all forward motion invites the
next session to reopen a question this one already closed, or to "fix" something that was
left alone on purpose. One line naming what you skipped, and why, is worth more than another
paragraph of plan.

Read the draft back before recording it, and check that it answers all five:

1. Which branch, and is the work pushed or only committed?
2. What exact command proves the tree is green, and what did it actually say?
3. Which decision is settled and must not be relitigated?
4. Which trap costs the next session an hour if nobody mentions it?
5. Could someone with no memory of this session start work in under two minutes?

A "no" is not a reason to abandon the handoff — it is the thing to go and find out, or to
state as unknown. An unknown written down is useful; a guess written as fact is not.

## 2. Close the thread you were handed

If this session was launched from a handoff, say what became of it. `bridge
origin` prints that handoff's id and nothing else, so it substitutes directly:

```
done     you finished it
partial  you made real progress and the rest is in the prompt you are writing
dropped  you did not do it, and the next session should know why not
```

A session started by hand has no origin; `bridge origin` exits non-zero with
empty output and the flags below simply disappear. Do not invent one.

## 3. Record

Run exactly this, substituting your summary, your prompt, and — if there is an
origin — the outcome:

```bash
# Empty for a session nobody launched from a handoff, which is the normal case.
closes=$(bridge origin 2>/dev/null || true)
outcome=done          # done | partial | dropped — only read when `closes` is set

summary=$(cat <<'BRIDGE_SUMMARY'
<your one-line summary>
BRIDGE_SUMMARY
)
bridge handoff \
  --summary "$summary" \
  --session-id "$CLAUDE_CODE_SESSION_ID" \
  --effort "$CLAUDE_EFFORT" \
  ${closes:+--closes "$closes" --outcome "$outcome"} \
  --prompt-file - <<'BRIDGE_PROMPT'
<your next-session prompt, as many paragraphs as it needs>
BRIDGE_PROMPT
```

`${closes:+…}` expands to nothing at all when `closes` is empty, so one form of
the command covers both cases. Its result is word-split, which is safe here and
only here: a handoff id is a UUID, so it cannot contain a space. Do not reuse
that shape for the summary or the prompt.

## 4. Say what kind of handoff it is

Add `--kind` when this is not an ordinary continuation:

- **`--kind blocked`** — nothing can proceed until a *person* does something:
  a credential, a purchase, an access grant, a decision only they can take.
  Bridge surfaces these separately, as things waiting on them rather than work
  waiting for a session. Use it for the thing you would otherwise have written
  down somewhere they will never look.
- **`--kind parked`** — a real idea, deliberately deferred. It stays on the
  project and stops asking to be done.

The default is `next`, which is what almost everything is. Reach for `blocked`
only when a session genuinely cannot make the next move alone — a handoff that
needs a decision but could still be *started* is a `next` that says so.

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

## 5. Interpret the exit status

- **Exit 0, stderr says `queued for <path>`** — recorded in the panel. Done.
- **Exit 0, stderr says `spooled to <path>`** — also success. The panel is not running,
  which is the normal case; the handoff is on disk and the server ingests it the next time
  it starts. Report it as captured, not as a failure.
- **Exit 0, stderr prints the prompt back** — spooling itself failed. The prompt survives in
  this transcript only. Tell the user plainly and paste the path you tried.
- **Non-zero** — a real failure. The usual cause is an empty prompt, which exits 2. Fix and
  rerun; nothing was recorded.

## 6. Confirm

Tell the user in one line what was captured and where it went. If the panel is running, they
can see it at http://127.0.0.1:8787; otherwise mention it will appear on next `bridge serve`.

Worth mentioning once, if they do not already know it: from a terminal in the
project directory, `bridge resume` runs the newest queued handoff right there,
in that window, rather than opening a new one.

## Installation note

This file lives in the Bridge repo at `commands/handoff.md` and must be copied to
`~/.claude/commands/handoff.md` to be usable. Bridge never writes outside `~/.bridge`, so
that copy is a deliberate manual step:

```bash
cp ~/dev/bridge/commands/handoff.md ~/.claude/commands/handoff.md
```
