"""Settings read model (Task 5.1): effective configuration + hook status.

Model only -- the `/settings` route, template, and JS land in Task 5.2. These
tests exercise `build_settings` directly, the same way `test_schedule_route.py`'s
model section exercises `build_schedule` before its route half exists.
"""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from bridge.api import create_app
from bridge.config import load
from bridge.settings_view import build_settings
from bridge.store import Store


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


# --- Task 5.2: the `/settings` route --------------------------------------


def _route_client(tmp_path, **cfg_overrides):
    cfg = _cfg(tmp_path, **cfg_overrides)
    store = Store(cfg.db_path)
    return TestClient(create_app(store, cfg)), cfg


def test_settings_route_returns_200_with_exactly_one_h1(tmp_path):
    client, _ = _route_client(tmp_path)

    resp = client.get("/settings")

    assert resp.status_code == 200
    assert len(re.findall(r"<h1[ >]", resp.text)) == 1


def test_settings_route_renders_the_three_preference_groups(tmp_path):
    client, _ = _route_client(tmp_path)

    resp = client.get("/settings")

    for hook in (
        "data-settings-theme", "data-settings-density",
        "data-settings-launch-model", "data-settings-launch-effort",
        "data-settings-launch-mode",
    ):
        assert hook in resp.text, f"missing {hook}"


def test_settings_route_renders_effective_configuration_and_hook_status(tmp_path):
    client, cfg = _route_client(tmp_path)

    resp = client.get("/settings")

    assert str(cfg.db_path) in resp.text
    assert str(cfg.claude_projects_dir) in resp.text
    assert str(cfg.stale_hours) in resp.text
    # No settings.json written -- the guarded default path is empty, so the
    # fresh-install "absent" wording and guidance must show up.
    assert "not installed" in resp.text.lower()


def test_settings_route_marks_nav_entry_current(tmp_path):
    client, _ = _route_client(tmp_path)

    resp = client.get("/settings")

    assert re.search(r'href="/settings"\s+aria-current="page"', resp.text)
    # The nav entry only lights up on its own page, not on another route.
    other = client.get("/")
    assert not re.search(r'href="/settings"\s+aria-current="page"', other.text)


def test_settings_route_has_no_write_endpoint(tmp_path):
    client, _ = _route_client(tmp_path)

    for method in (client.post, client.put, client.patch, client.delete):
        resp = method("/settings")
        assert resp.status_code == 405, f"{method.__name__} unexpectedly allowed"


def test_settings_route_never_reads_the_real_claude_settings_json(tmp_path, monkeypatch):
    """Proves the conftest guard is actually wired in for the ROUTE's own
    call, not just for a test that remembers to pass `settings_path` itself:
    even with `Path.read_text` spying on the developer's real file, hitting
    the route never touches it."""
    real_path = Path.home() / ".claude" / "settings.json"
    touched = []
    orig_read_text = Path.read_text

    def spy(self, *args, **kwargs):
        if self == real_path:
            touched.append(self)
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    client, _ = _route_client(tmp_path)

    resp = client.get("/settings")

    assert resp.status_code == 200
    assert not touched, "GET /settings read the developer's real ~/.claude/settings.json"


def test_settings_route_reflects_hook_status_from_the_guarded_default_path(
    tmp_path, guarded_claude_settings_path,
):
    """The guard substitutes a stand-in path rather than skipping the read
    altogether -- writing hooks to that exact path must still show up as
    "present" through the route, proving the route's real (unpatched)
    default-path code path is what ran, not a short-circuit."""
    client, cfg = _route_client(tmp_path, port=9321)
    url = f"http://127.0.0.1:{cfg.port}/api/hooks"
    hooks = {
        name: {"hooks": [{"type": "http", "url": url, "timeout": 2}]}
        for name in ("Notification", "SessionStart", "SessionEnd")
    }
    guarded_claude_settings_path.write_text(json.dumps({"hooks": hooks}))

    resp = client.get("/settings")

    assert "not installed" not in resp.text.lower()
