# Contributing

## Dev setup

```bash
git clone https://github.com/mit112/bridge.git
cd bridge
uv sync --extra dev
uv run bridge setup
```

`uv sync` does not put `.venv/bin` on your PATH, so use `uv run bridge …` (or
activate the venv) when working from a clone.

## Running tests

```bash
uv run pytest
```

The suite is **hermetic** — it must pass under a clean `$HOME` with no real
Claude Code transcript corpus present. If a test only passes because of your
own `~/.claude` data, that's a bug in the test, not a pass.

## Architecture

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before touching anything
non-trivial. It covers the module boundaries, the sole-writer database model,
the incremental indexer, and the invariants tests rely on.

## PR expectations

- Tests green (`uv run pytest`), including any you add for the change.
- One logical change per PR — don't bundle an unrelated fix or refactor in
  with a feature.
- Match the existing code's voice: comments explain *why*, not what the code
  already says.
- If you're touching a documented invariant (see ARCHITECTURE.md), the PR
  description should say so explicitly.
