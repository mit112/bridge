"""Configuration for Bridge. Overrides are for tests; there is no config file yet."""

from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_MODELS = ["opus", "sonnet", "haiku"]
DEFAULT_EFFORTS = ["low", "medium", "high"]


@dataclass(frozen=True)
class Config:
    claude_projects_dir: Path
    db_path: Path
    dev_dir: Path
    stale_hours: int
    models: list[str]
    efforts: list[str]
    port: int


def load(overrides: dict | None = None) -> Config:
    home = Path.home()
    cfg = Config(
        claude_projects_dir=home / ".claude" / "projects",
        db_path=home / ".bridge" / "bridge.db",
        dev_dir=home / "dev",
        stale_hours=12,
        models=list(DEFAULT_MODELS),
        efforts=list(DEFAULT_EFFORTS),
        port=8787,
    )
    if overrides:
        cfg = replace(cfg, **overrides)
    return cfg
