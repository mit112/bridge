"""Configuration for Bridge. Overrides are for tests; there is no config file yet."""

import os
from dataclasses import dataclass, replace
from pathlib import Path

@dataclass(frozen=True)
class ModelChoice:
    """One entry in the launch band's model selector.

    `value` goes to `--model` verbatim and is never validated here: the CLI is
    the authority on what it accepts, exactly as with `effort`. `label` exists
    because "opus" alone cannot express "pin me to 4.8" — the alias floats to
    whatever is newest, which is the right default and the wrong record.
    """

    value: str
    label: str


# Ids verified against `claude` 2.1.220 on this machine. Aliases first (the
# common case, and the default selection), then pinned versions newest-first.
DEFAULT_MODELS = [
    ModelChoice("opus", "opus — latest (Opus 5)"),
    ModelChoice("claude-opus-5", "Opus 5"),
    ModelChoice("claude-opus-4-8", "Opus 4.8"),
    ModelChoice("claude-opus-4-7", "Opus 4.7"),
    ModelChoice("sonnet", "sonnet — latest (Sonnet 5)"),
    ModelChoice("claude-sonnet-5", "Sonnet 5"),
    ModelChoice("claude-sonnet-4-6", "Sonnet 4.6"),
    ModelChoice("haiku", "haiku — latest (Haiku 4.5)"),
    ModelChoice("claude-haiku-4-5", "Haiku 4.5"),
    ModelChoice("fable", "fable — latest (Fable 5)"),
    ModelChoice("claude-fable-5", "Fable 5"),
]
# The full set `claude --effort` accepts. Phase 3 is the first phase to surface
# the list in the UI, so the two values nothing read before are added here rather
# than left implicit. The spec's `~/.bridge/config.toml` is still out of scope.
DEFAULT_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Projects move, and a transcript records the cwd it was written under, so one
# logical project can appear under several paths with its history split between
# them. These are the verified old locations on this machine; every target was
# confirmed present on disk. Home-relative here, absolute once `load` expands
# them, because that is the form a transcript `cwd` takes.
DEFAULT_ALIASES = {
    "Documents/Job apps": "dev/Job apps",
    "Documents/projectX": "dev/projectX",
    "Documents/projectX/hookrail": "dev/projectX/hookrail",
    "Documents/claude-stuff/dota2": "dev/claude-stuff/dota2",
    "Documents/claude-stuff/Houston social": "dev/claude-stuff/Houston social",
    # A rename as well as a move: the two spellings genuinely differ.
    "Documents/anhkhooey": "dev/anghkooey",
    # A deleted worktree folded back into its parent repo: the work landed in
    # the parent, so its sessions belong there.
    "dev/StreakSync/.worktrees/streaksync-ui-polish": "dev/StreakSync",
}

# Paths with no alias target because the directory is gone entirely. Archived,
# never deleted.
DEFAULT_ARCHIVED = ("Documents/Vandit & Zeel/VANDITZEEL",)


@dataclass(frozen=True)
class PermissionChoice:
    """One entry in the launch band's permission selector.

    An empty `value` means *emit no flag at all*, which is the default and the
    only safe one. `danger` drives the conspicuous styling: the affordance must
    never read as ordinary, and must say so in words as well as colour.
    """

    value: str
    label: str
    danger: bool = False


# Measured against `claude` 2.1.220, which rejects anything else with
# "Allowed choices are acceptEdits, auto, bypassPermissions, manual, dontAsk,
# plan". There is deliberately no `default` here: that value belongs to
# settings.json's `permissions.defaultMode` (a different, smaller set --
# `"default" | "plan" | "acceptEdits" | "dontAsk"`), and passing it to
# `--permission-mode` fails the launch outright. The two are easy to conflate.
#
# The no-flag entry is first because it is what an unsuggested launch selects.
DEFAULT_PERMISSION_MODES = [
    PermissionChoice("", "Ask as usual"),
    PermissionChoice("plan", "plan — plan first, no edits"),
    PermissionChoice("acceptEdits", "acceptEdits — auto-accept file edits"),
    PermissionChoice("dontAsk", "dontAsk — stop prompting"),
    PermissionChoice("auto", "auto"),
    PermissionChoice("manual", "manual"),
    PermissionChoice(
        "bypassPermissions", "bypassPermissions — SKIP ALL CHECKS", danger=True
    ),
]


@dataclass(frozen=True)
class Config:
    claude_projects_dir: Path
    db_path: Path
    spool_dir: Path
    # Where a launch's prompt file is written, mode 0700. The prompt is taken off
    # the command line entirely, so this directory is the only place it exists
    # between `launch()` and the new shell's `cat`.
    launches_dir: Path
    dev_dir: Path
    stale_hours: int
    models: list[ModelChoice]
    efforts: list[str]
    permission_modes: list[PermissionChoice]
    port: int
    aliases: dict[str, str]
    archived_paths: tuple[str, ...]


def load(overrides: dict | None = None) -> Config:
    home = Path.home()
    cfg = Config(
        claude_projects_dir=home / ".claude" / "projects",
        db_path=home / ".bridge" / "bridge.db",
        spool_dir=home / ".bridge" / "spool",
        launches_dir=home / ".bridge" / "launches",
        dev_dir=home / "dev",
        stale_hours=12,
        models=list(DEFAULT_MODELS),
        efforts=list(DEFAULT_EFFORTS),
        permission_modes=list(DEFAULT_PERMISSION_MODES),
        # Env-overridable so the CLI's exit-zero-when-the-panel-is-down property
        # can be tested in a real subprocess against a genuinely closed port,
        # rather than against a mocked transport.
        port=int(os.environ.get("BRIDGE_PORT") or 8787),
        aliases={f"{home}/{a}": f"{home}/{c}" for a, c in DEFAULT_ALIASES.items()},
        archived_paths=tuple(f"{home}/{p}" for p in DEFAULT_ARCHIVED),
    )
    if overrides:
        cfg = replace(cfg, **overrides)
    return cfg
