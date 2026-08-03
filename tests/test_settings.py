"""Settings read model (Task 5.1): effective configuration + hook status.

Model only -- the `/settings` route, template, and JS land in Task 5.2. These
tests exercise `build_settings` directly, the same way `test_schedule_route.py`'s
model section exercises `build_schedule` before its route half exists.
"""

import json
from pathlib import Path

from bridge.config import load
from bridge.settings_view import build_settings


def _cfg(tmp_path, **overrides):
    defaults = dict(
        db_path=tmp_path / "bridge.db",
        spool_dir=tmp_path / "spool",
        session_meta_dir=tmp_path / "session-meta",
        claude_projects_dir=tmp_path / "claude-projects",
        stale_hours=6,
        aliases={"/p/one": "/real/one"},
        archived_paths=("/real/archived",),
        port=9999,
    )
    defaults.update(overrides)
    return load(defaults)


def _write_settings(tmp_path, port=9999, events=("Notification", "SessionStart", "SessionEnd")):
    """A `~/.claude/settings.json` shaped exactly like the real one Task 9 of
    the phase-4 amendments installed (recon §3b): one `{"hooks": [...]}` list
    per event, each entry `{"type": "http", "url": ..., "timeout": 2}`."""
    url = f"http://127.0.0.1:{port}/api/hooks"
    hooks = {
        name: {"hooks": [{"type": "http", "url": url, "timeout": 2}]}
        for name in events
    }
    path = tmp_path / "claude-settings.json"
    path.write_text(json.dumps({"hooks": hooks, "allowedHttpHookUrls": [url]}))
    return path


def test_effective_fields_come_from_config(tmp_path):
    cfg = _cfg(tmp_path)

    model = build_settings(cfg, settings_path=tmp_path / "no-such-settings.json")

    assert model.claude_projects_dir == cfg.claude_projects_dir
    assert model.session_meta_dir == cfg.session_meta_dir
    assert model.stale_hours == 6
    assert model.aliases == {"/p/one": "/real/one"}
    assert model.archived_paths == ("/real/archived",)
    assert model.db_path == cfg.db_path
    assert model.port == 9999


def test_config_path_reflects_bridge_config_env(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    fake_config_path = tmp_path / "config.toml"
    monkeypatch.setenv("BRIDGE_CONFIG", str(fake_config_path))

    model = build_settings(cfg, settings_path=tmp_path / "absent.json")

    assert model.config_path == fake_config_path


def test_hook_status_present_when_all_three_hooks_point_at_this_port(tmp_path):
    cfg = _cfg(tmp_path, port=9999)
    settings_path = _write_settings(tmp_path, port=9999)

    model = build_settings(cfg, settings_path=settings_path)

    assert model.hook_status.state == "present"
    assert all(e.installed for e in model.hook_status.events)
    assert model.hook_status.issues == ()


def test_hook_status_absent_when_settings_file_missing(tmp_path):
    cfg = _cfg(tmp_path)
    settings_path = tmp_path / "does-not-exist.json"

    model = build_settings(cfg, settings_path=settings_path)

    assert model.hook_status.state == "absent"
    assert model.hook_status.issues
    assert "not installed" in model.hook_status.issues[0]["cause"].lower()
    assert model.hook_status.issues[0]["next_action"]


def test_hook_status_absent_when_settings_file_is_malformed_json(tmp_path):
    cfg = _cfg(tmp_path)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{not valid json")

    model = build_settings(cfg, settings_path=settings_path)

    assert model.hook_status.state == "absent"
    assert model.hook_status.issues


def test_hook_status_absent_when_hooks_key_has_none_of_the_three_events(tmp_path):
    cfg = _cfg(tmp_path)
    settings_path = tmp_path / "settings.json"
    # A real settings.json always has these four unrelated to Bridge (recon
    # §3b) -- must not be mistaken for Bridge's own hooks.
    settings_path.write_text(json.dumps({"hooks": {"UserPromptSubmit": {}}}))

    model = build_settings(cfg, settings_path=settings_path)

    assert model.hook_status.state == "absent"


def test_hook_status_partial_when_one_hook_missing(tmp_path):
    cfg = _cfg(tmp_path, port=9999)
    settings_path = _write_settings(
        tmp_path, port=9999, events=("Notification", "SessionStart")
    )

    model = build_settings(cfg, settings_path=settings_path)

    assert model.hook_status.state == "partial"
    installed = {e.name for e in model.hook_status.events if e.installed}
    assert installed == {"Notification", "SessionStart"}
    assert "SessionEnd" in model.hook_status.issues[0]["cause"]


def test_hook_status_absent_when_hooks_point_at_a_different_port(tmp_path):
    cfg = _cfg(tmp_path, port=9999)
    # Installed, but wired to a stale port (e.g. BRIDGE_PORT changed since
    # the hooks were set up) -- a real, catchable misconfiguration per recon.
    settings_path = _write_settings(tmp_path, port=8787)

    model = build_settings(cfg, settings_path=settings_path)

    assert model.hook_status.state == "absent"
    assert all(not e.installed for e in model.hook_status.events)


def test_launch_defaults_expose_catalogs_with_ask_as_usual_first(tmp_path):
    cfg = _cfg(tmp_path)

    model = build_settings(cfg, settings_path=tmp_path / "absent.json")

    assert model.launch_defaults.models == cfg.models
    assert model.launch_defaults.efforts == cfg.efforts
    assert model.launch_defaults.permission_modes == cfg.permission_modes
    assert model.launch_defaults.permission_modes[0].value == ""
    assert model.launch_defaults.permission_modes[0].label == "Ask as usual"


def test_default_settings_path_never_reads_the_real_claude_settings(tmp_path, monkeypatch):
    """The injectable seam must be the only path read when omitted -- a caller
    who forgets to pass `settings_path` must land on a stand-in `Path.home()`,
    never the developer's real `~/.claude/settings.json`."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    cfg = _cfg(tmp_path)

    model = build_settings(cfg)

    assert model.hook_status.state == "absent"
    assert not (tmp_path / ".claude" / "settings.json").exists()
