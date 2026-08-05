"""Tests for `bridge setup` — the installer that writes to the user's real
machine (`~/.bridge`, `~/Library/LaunchAgents`, launchd). Every test here drives
it against an isolated temp HOME with launchctl stubbed, because the module's
job is precisely to touch dangerous real paths and the point of the tests is to
prove it touches only the scoped ones.

`setup.py` binds its target paths as module-level constants at import time
(`BRIDGE_DIR = Path.home() / ".bridge"` etc.), so isolation is per-test
rebinding of those constants rather than a HOME env var, which would arrive too
late to matter.
"""

import tomllib

import pytest

from bridge import setup


class _R:
    """Minimal stand-in for a `subprocess.CompletedProcess`."""

    def __init__(self, returncode, stderr):
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Rebind every real path `setup` writes to into an isolated temp HOME.

    Returns the temp HOME. `LAUNCHD_AGENTS_DIR` gets a sibling plist for a
    pretend other app so the uninstall tests can prove scoping.
    """
    h = tmp_path / "home"
    bridge_dir = h / ".bridge"
    agents_dir = h / "Library" / "LaunchAgents"
    commands_dir = h / ".claude" / "commands"
    for d in (bridge_dir, agents_dir, commands_dir):
        d.mkdir(parents=True)

    monkeypatch.setattr(setup, "HOME", h)
    monkeypatch.setattr(setup, "BRIDGE_DIR", bridge_dir)
    monkeypatch.setattr(setup, "CONFIG_PATH", bridge_dir / "config.toml")
    monkeypatch.setattr(setup, "LAUNCHD_PLIST_PATH",
                        bridge_dir / setup.LAUNCHD_PLIST_NAME)
    monkeypatch.setattr(setup, "LAUNCHD_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(setup, "CLAUDE_COMMANDS_DIR", commands_dir)
    monkeypatch.setattr(setup, "HANDOFF_DEST", commands_dir / "handoff.md")

    # A LaunchAgent that is not Bridge's, to prove uninstall never touches it.
    (agents_dir / "com.example.other.plist").write_text("<plist/>")
    return h


@pytest.fixture
def launchctl(monkeypatch):
    """Record every `launchctl` invocation instead of running it. Success by
    default; set `.returncode`/`.stderr` on the returned recorder to simulate a
    failure."""

    class Recorder:
        def __init__(self):
            self.calls = []
            self.returncode = 0
            self.stderr = ""

        def __call__(self, argv, **kwargs):
            self.calls.append(argv)
            # The wait-for-unload poll shells out to `launchctl print`; a
            # non-zero exit is how it learns the label is gone. Report unloaded
            # immediately so the loop returns without sleeping.
            if argv[:2] == ["launchctl", "print"]:
                return _R(1, "")
            if kwargs.get("check") and self.returncode != 0:
                raise setup.subprocess.CalledProcessError(
                    self.returncode, argv, stderr=self.stderr
                )
            return _R(self.returncode, self.stderr)

    rec = Recorder()
    monkeypatch.setattr(setup.subprocess, "run", rec)
    return rec


def _answers(monkeypatch, *values):
    """Feed `_ask_yn` a fixed sequence of yes/no answers, in order."""
    it = iter(values)
    monkeypatch.setattr(setup, "_ask_yn", lambda *a, **k: next(it))


# ── port selection idempotency ───────────────────────────────────────────────


class _FakeResp:
    """Stand-in for `urllib.request.urlopen`'s context-manager response."""

    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def read(self):  # json.load reads through this
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_configured_port_reads_the_recorded_port(home, monkeypatch):
    setup.CONFIG_PATH.write_text("port = 8790\n")
    monkeypatch.setenv("BRIDGE_CONFIG", str(setup.CONFIG_PATH))
    assert setup._configured_port() == 8790


def test_configured_port_is_none_without_a_config(home, monkeypatch):
    monkeypatch.setenv("BRIDGE_CONFIG", str(setup.CONFIG_PATH))  # file absent
    assert setup._configured_port() is None


def test_configured_port_is_none_when_config_omits_port(home, monkeypatch):
    setup.CONFIG_PATH.write_text("[discovery]\npaths = []\n")
    monkeypatch.setenv("BRIDGE_CONFIG", str(setup.CONFIG_PATH))
    assert setup._configured_port() is None


def test_configured_port_is_none_when_config_is_malformed(home, monkeypatch):
    setup.CONFIG_PATH.write_text("port = = = broken")
    monkeypatch.setenv("BRIDGE_CONFIG", str(setup.CONFIG_PATH))
    assert setup._configured_port() is None


def test_bridge_is_serving_true_for_a_bridge_diagnostics_response(monkeypatch):
    import urllib.request

    payload = b'{"live": "ok", "live_source": "registry", "spool_depth": 0}'
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(200, payload))
    assert setup._bridge_is_serving(8787) is True


def test_bridge_is_serving_false_for_a_non_bridge_json_response(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(200, b'{"hello": "world"}'))
    assert setup._bridge_is_serving(8787) is False


def test_bridge_is_serving_false_when_nothing_answers(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert setup._bridge_is_serving(8787) is False


def test_step_port_keeps_free_default_on_fresh_install(monkeypatch):
    monkeypatch.setattr(setup, "_port_is_free", lambda p: True)
    assert setup._step_port(None) == setup.DEFAULT_PORT


def test_step_port_keeps_the_configured_port_when_free(monkeypatch):
    monkeypatch.setattr(setup, "_port_is_free", lambda p: True)
    assert setup._step_port(8790) == 8790


def test_step_port_keeps_the_port_when_our_own_panel_holds_it(monkeypatch):
    # The idempotency fix: the configured port reads "in use", but it is our own
    # already-running panel — keep it, and never prompt to migrate.
    monkeypatch.setattr(setup, "_port_is_free", lambda p: False)
    monkeypatch.setattr(setup, "_bridge_is_serving", lambda p, **k: True)
    asked = []
    monkeypatch.setattr(setup, "_ask",
                        lambda *a, **k: asked.append(1) or "8788")
    assert setup._step_port(8787) == 8787
    assert asked == []  # no migration prompt


def test_step_port_offers_migration_when_a_foreign_process_holds_it(monkeypatch):
    # A non-Bridge process on the configured port still triggers the offer.
    monkeypatch.setattr(setup, "_port_is_free", lambda p: p != 8787)
    monkeypatch.setattr(setup, "_bridge_is_serving", lambda p, **k: False)
    monkeypatch.setattr(setup, "_ask", lambda *a, **k: "8788")
    assert setup._step_port(8787) == 8788


# ── plist generation ─────────────────────────────────────────────────────────


def test_generate_plist_fills_the_live_paths(home):
    xml = setup._generate_plist("/venv/bin/python", 8790, "/opt/claude/bin")

    assert f"<string>{setup.LAUNCHD_LABEL}</string>" in xml
    # The interpreter that will run the server, verbatim in ProgramArguments.
    assert "<string>/venv/bin/python</string>" in xml
    assert "<string>-m</string>" in xml
    # Port is handed to the server through BRIDGE_PORT.
    assert "<string>8790</string>" in xml
    # claude's directory is prepended to PATH so session launches resolve it.
    assert "/opt/claude/bin" in xml
    # Logs and working dir resolve under the (temp) HOME / BRIDGE_DIR.
    assert str(setup.BRIDGE_DIR / "serve.log") in xml
    assert f"<string>{home}</string>" in xml


def test_generate_plist_escapes_xml_special_characters(home):
    xml = setup._generate_plist("/venv/A&B/python", 8787, None)
    assert "/venv/A&amp;B/python" in xml
    assert "/venv/A&B/python" not in xml


def test_generate_plist_without_claude_still_produces_valid_path(home):
    # claude_dir=None must not put a literal "None" into PATH.
    xml = setup._generate_plist("/venv/bin/python", 8787, None)
    assert "None" not in xml
    assert "/usr/bin" in xml


# ── config generation ────────────────────────────────────────────────────────


def test_generate_config_writes_discovery_and_port_as_valid_toml():
    text = setup._generate_config(["/Users/me/dev", "/Users/me/work"], 8791)
    data = tomllib.loads(text)
    assert data["discovery"]["paths"] == ["/Users/me/dev", "/Users/me/work"]
    assert data["port"] == 8791


def test_generate_config_omits_discovery_table_when_no_paths():
    text = setup._generate_config([], 8787)
    data = tomllib.loads(text)
    assert "discovery" not in data
    assert data["port"] == 8787


def test_generated_config_round_trips_through_config_load(tmp_path, monkeypatch):
    """The file `setup` writes must be one `config.load` actually understands."""
    text = setup._generate_config([str(tmp_path / "dev")], 8792)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(text)
    monkeypatch.setenv("BRIDGE_CONFIG", str(cfg_file))
    monkeypatch.delenv("BRIDGE_PORT", raising=False)

    from bridge.config import load

    cfg = load()
    assert cfg.port == 8792
    assert cfg.discovery_paths == (tmp_path / "dev",)


# ── --launchd-only ───────────────────────────────────────────────────────────


def test_launchd_only_regenerates_the_plist_with_the_configured_port(
    home, launchctl, monkeypatch
):
    # A config.toml carrying the port the installer recorded earlier.
    setup.CONFIG_PATH.write_text("port = 8795\n")
    monkeypatch.setenv("BRIDGE_CONFIG", str(setup.CONFIG_PATH))
    monkeypatch.delenv("BRIDGE_PORT", raising=False)
    monkeypatch.setattr(setup, "_resolve_claude_path", lambda: "/opt/claude/bin")
    _answers(monkeypatch, False)  # decline the reinstall; just prove the write

    rc = setup.run_launchd_only()

    assert rc == 0
    assert setup.LAUNCHD_PLIST_PATH.exists()
    xml = setup.LAUNCHD_PLIST_PATH.read_text()
    assert "<string>8795</string>" in xml  # port recovered from config.toml
    assert launchctl.calls == []  # declined → launchctl never touched


def test_launchd_only_reinstall_bootstraps_the_bridge_label(
    home, launchctl, monkeypatch
):
    setup.CONFIG_PATH.write_text("port = 8787\n")
    monkeypatch.setenv("BRIDGE_CONFIG", str(setup.CONFIG_PATH))
    monkeypatch.setattr(setup, "_resolve_claude_path", lambda: None)
    _answers(monkeypatch, True)  # accept the reinstall

    rc = setup.run_launchd_only()

    assert rc == 0
    # The plist is copied into LaunchAgents and bootstrapped by label.
    assert (setup.LAUNCHD_AGENTS_DIR / setup.LAUNCHD_PLIST_NAME).exists()
    # Reinstall must boot out any already-loaded instance BEFORE bootstrap,
    # otherwise launchctl fails with EIO against a running agent. (The
    # wait-for-unload poll's `print` calls sit between the two and are ignored.)
    verbs = [c[1] for c in launchctl.calls if c[1] in ("bootout", "bootstrap")]
    assert verbs == ["bootout", "bootstrap"]
    bootstrap = next(c for c in launchctl.calls if c[1] == "bootstrap")
    assert str(setup.LAUNCHD_AGENTS_DIR / setup.LAUNCHD_PLIST_NAME) in bootstrap


# ── --uninstall (the dangerous one) ──────────────────────────────────────────


def test_uninstall_declining_removal_preserves_the_bridge_dir(
    home, launchctl, monkeypatch
):
    (setup.BRIDGE_DIR / "bridge.db").write_text("precious")
    (setup.LAUNCHD_AGENTS_DIR / setup.LAUNCHD_PLIST_NAME).write_text("<plist/>")

    _answers(monkeypatch, False, False)  # keep ~/.bridge; keep handoff

    rc = setup.run_uninstall()

    assert rc == 0
    # Data is preserved when the user says no.
    assert (setup.BRIDGE_DIR / "bridge.db").read_text() == "precious"
    # The LaunchAgent, however, is always torn down.
    assert not (setup.LAUNCHD_AGENTS_DIR / setup.LAUNCHD_PLIST_NAME).exists()


def test_uninstall_removal_is_scoped_to_bridges_own_paths(
    home, launchctl, monkeypatch
):
    (setup.BRIDGE_DIR / "bridge.db").write_text("data")
    (setup.LAUNCHD_AGENTS_DIR / setup.LAUNCHD_PLIST_NAME).write_text("<plist/>")
    setup.HANDOFF_DEST.write_text("handoff")
    # A neighbour that must survive: a sibling of ~/.bridge under HOME.
    neighbour = home / "keep-me"
    neighbour.mkdir()
    (neighbour / "file").write_text("safe")

    _answers(monkeypatch, True, True)  # remove ~/.bridge; remove handoff

    rc = setup.run_uninstall()

    assert rc == 0
    assert not setup.BRIDGE_DIR.exists()
    assert not setup.HANDOFF_DEST.exists()
    # Only Bridge's own launch agent went; the other app's plist stays.
    assert not (setup.LAUNCHD_AGENTS_DIR / setup.LAUNCHD_PLIST_NAME).exists()
    assert (setup.LAUNCHD_AGENTS_DIR / "com.example.other.plist").exists()
    # Nothing outside ~/.bridge was touched.
    assert (neighbour / "file").read_text() == "safe"


def test_uninstall_launchd_is_a_noop_when_nothing_is_installed(home, launchctl):
    assert setup._uninstall_launchd() is False
    # No bootout attempted when there is no plist to remove.
    assert launchctl.calls == []


def test_uninstall_launchd_boots_out_the_bridge_label(home, launchctl):
    (setup.LAUNCHD_AGENTS_DIR / setup.LAUNCHD_PLIST_NAME).write_text("<plist/>")

    assert setup._uninstall_launchd() is True
    boots = [c for c in launchctl.calls if c[:2] == ["launchctl", "bootout"]]
    assert len(boots) == 1
    assert f"gui/" in boots[0][2] and setup.LAUNCHD_LABEL in boots[0][2]
