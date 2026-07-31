from pathlib import Path

from bridge.config import Config, load


def test_load_returns_defaults():
    cfg = load()
    assert cfg.claude_projects_dir == Path.home() / ".claude" / "projects"
    assert cfg.db_path == Path.home() / ".bridge" / "bridge.db"
    assert cfg.stale_hours == 12
    assert cfg.port == 8787
    assert "high" in cfg.efforts
    assert cfg.models  # non-empty


def test_overrides_win(tmp_path):
    cfg = load({"db_path": tmp_path / "x.db", "stale_hours": 3})
    assert cfg.db_path == tmp_path / "x.db"
    assert cfg.stale_hours == 3
    # unspecified fields keep defaults
    assert cfg.port == 8787


def test_config_is_frozen():
    cfg = load()
    try:
        cfg.stale_hours = 99
    except Exception:
        return
    raise AssertionError("Config must be immutable")
