# Bridge

A local control panel for Claude Code projects — see each project's state, keep the
next-session prompt with it, and launch the session from the card.

**macOS only.** Bridge uses `osascript` to spawn Terminal windows and runs as a
LaunchAgent. There is no Linux or Windows support.

## Quick start

```bash
# 1. Install
uv tool install git+https://github.com/mit112/bridge

# 2. Set up (interactive — takes ~1 minute)
bridge setup

# 3. Index your transcripts
bridge index

# 4. Open the panel
bridge open
```

`bridge setup` walks you through everything: it finds your project directories,
picks a port, optionally installs the `/handoff` slash command, and optionally
sets up a LaunchAgent so the panel stays running.

## What you get

- **Dashboard** at http://127.0.0.1:8787 — every Claude Code project as a card
  showing git branch/dirtiness, last session, token usage sparklines, and live
  session status
- **Handoff loop** — end a session with `/handoff` to capture a summary and
  next-session prompt; launch it from the card with one click
- **Launch bar** — pick model, effort, and permission mode per launch
- **Live updates** — SSE-pushed liveness, git state, and diagnostics
- **Scheduled runs** — set a time and Bridge launches the session for you

## Prerequisites

- macOS (Bridge uses `osascript` and LaunchAgents)
- Python ≥ 3.13
- [uv](https://docs.astral.sh/uv/) (for `uv tool install`)
- [Claude Code](https://claude.ai/code) (`claude` on your PATH)

## Install from source (for contributors)

```bash
git clone https://github.com/mit112/bridge.git
cd bridge
uv sync --extra dev
bridge setup
```

## The handoff loop

End a Claude Code session by running `/handoff`. It composes a summary and a
next-session prompt and records them against the current project. `bridge next`
prints the prompt back, so `claude "$(bridge next)"` opens the next session on it.

`bridge setup` installs the slash command for you (`~/.claude/commands/handoff.md`).
To install it manually:

```bash
cp ~/dev/bridge/commands/handoff.md ~/.claude/commands/handoff.md
```

The handoff is durable: if the panel is down, `bridge handoff` spools the prompt
to `~/.bridge/spool/` and the server ingests it on the next boot.

## Launching a session

Press ▶ on a card to open the queued prompt as a real session in a new Terminal
window, with the model and effort chosen beside the button. From the shell:

```bash
bridge launch [--project P] [--mode terminal|background] [--model M] [--effort E]
```

## Configuration

`~/.bridge/config.toml` (created by `bridge setup`, optional):

```toml
# Directories to scan one level deep for git repos.
# Bridge auto-discovers these during `bridge index`.
[discovery]
paths = ["~/dev", "~/work"]

# Map old project paths to current ones so a moved project
# still shows as a single card.
[aliases]
"/Users/you/old-path" = "/Users/you/new-path"

# Paths to archive on first indexing (hide from the main list).
[archived]
paths = ["~/dev/retired-project"]

# Hours after which an uncommitted change becomes "stale".
[stale]
hours = 12

# Port the panel listens on (default 8787).
# BRIDGE_PORT env var takes precedence over this.
port = 8787
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `BRIDGE_PORT` | `8787` | Port the panel listens on |
| `BRIDGE_CONFIG` | `~/.bridge/config.toml` | Path to the config file |

## CLI reference

```
bridge handoff   Record a next-session prompt
bridge launch    Launch the queued prompt as a session
bridge next      Print the queued prompt to stdout
bridge status    Show panel and handoff state
bridge open      Open the panel in a browser
bridge setup     Interactive first-time setup
bridge index     Scan Claude Code transcripts
bridge serve     Start the panel manually
bridge backfill  Import stray HANDOFF.md / NEXT-SESSION.md files
```

## Scope and safety

Bridge never writes to a project repository. Its only writes are its own SQLite
database under `~/.bridge/`. All git access is read-only. It binds to localhost
only and has no authentication — it is a local tool for a local machine.

When you choose to enable the hooks feature (Phase 4), `bridge setup` will
guide you through adding three `type: "http"` hooks to `~/.claude/settings.json`
(`Notification`, `SessionStart`, `SessionEnd`) that POST to
`http://127.0.0.1:8787/api/hooks`. Each carries `timeout: 2` so a stopped
Bridge costs nothing — the connection is refused immediately.

## Uninstall

```bash
bridge setup --uninstall
```

This removes the LaunchAgent. You'll be asked whether to delete `~/.bridge/`
(all your Bridge data) and the `/handoff` slash command.

## Test

```bash
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE).
