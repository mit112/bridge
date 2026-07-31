"""Configuration for Bridge. Overrides are for tests; there is no config file yet."""

from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_MODELS = ["opus", "sonnet", "haiku"]
DEFAULT_EFFORTS = ["low", "medium", "high"]

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
class Config:
    claude_projects_dir: Path
    db_path: Path
    spool_dir: Path
    dev_dir: Path
    stale_hours: int
    models: list[str]
    efforts: list[str]
    port: int
    aliases: dict[str, str]
    archived_paths: tuple[str, ...]


def load(overrides: dict | None = None) -> Config:
    home = Path.home()
    cfg = Config(
        claude_projects_dir=home / ".claude" / "projects",
        db_path=home / ".bridge" / "bridge.db",
        spool_dir=home / ".bridge" / "spool",
        dev_dir=home / "dev",
        stale_hours=12,
        models=list(DEFAULT_MODELS),
        efforts=list(DEFAULT_EFFORTS),
        port=8787,
        aliases={f"{home}/{a}": f"{home}/{c}" for a, c in DEFAULT_ALIASES.items()},
        archived_paths=tuple(f"{home}/{p}" for p in DEFAULT_ARCHIVED),
    )
    if overrides:
        cfg = replace(cfg, **overrides)
    return cfg
