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


def test_default_aliases_are_the_verified_moved_project_mappings():
    """These seven cwds are old locations of projects that now live in ~/dev.
    Without the mapping each shows as a duplicate card with split history."""
    h = str(Path.home())
    assert load().aliases == {
        f"{h}/Documents/Job apps": f"{h}/dev/Job apps",
        f"{h}/Documents/projectX": f"{h}/dev/projectX",
        f"{h}/Documents/projectX/hookrail": f"{h}/dev/projectX/hookrail",
        f"{h}/Documents/claude-stuff/dota2": f"{h}/dev/claude-stuff/dota2",
        f"{h}/Documents/claude-stuff/Houston social":
            f"{h}/dev/claude-stuff/Houston social",
        # A rename as well as a move: the two spellings genuinely differ.
        f"{h}/Documents/anhkhooey": f"{h}/dev/anghkooey",
        # A deleted worktree folded back into its parent repo.
        f"{h}/dev/StreakSync/.worktrees/streaksync-ui-polish": f"{h}/dev/StreakSync",
    }


def test_vanditzeel_is_archived_because_it_has_no_alias_target():
    assert load().archived_paths == (
        f"{Path.home()}/Documents/Vandit & Zeel/VANDITZEEL",
    )
