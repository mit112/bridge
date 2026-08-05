from pathlib import Path

import pytest

from bridge.config import ConfigError, ModelChoice, load


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


# --- `~/.bridge/config.toml` --------------------------------------------------
#
# The autouse `never_read_the_real_config_file` fixture points `BRIDGE_CONFIG`
# at a path that does not exist, so a test wanting a file writes one and repoints
# the variable at it.


def write_config(tmp_path, monkeypatch, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    monkeypatch.setenv("BRIDGE_CONFIG", str(p))
    return p


def test_no_config_file_means_no_aliases_and_nothing_archived():
    """The fresh-install state, and not a degraded one.

    These two fields name one machine's directories. Having none of them is
    ordinary; the panel indexes every project under its own path and archives
    nothing.
    """
    cfg = load()
    assert cfg.aliases == {}
    assert cfg.archived_paths == ()


def test_aliases_are_read_from_the_config_file_and_made_absolute(tmp_path, monkeypatch):
    """Home-relative in the file, absolute in the Config.

    A transcript records an absolute `cwd`, so that is the form the alias table
    has to be keyed by for a lookup to ever hit.
    """
    write_config(tmp_path, monkeypatch, """
        [aliases]
        "Documents/old thing" = "dev/new thing"
    """)
    h = str(Path.home())
    assert load().aliases == {f"{h}/Documents/old thing": f"{h}/dev/new thing"}


def test_archived_paths_are_read_from_the_config_file(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, """
        [archived]
        paths = ["Documents/gone"]
    """)
    assert load().archived_paths == (f"{Path.home()}/Documents/gone",)


def test_an_absolute_path_in_the_file_is_left_alone(tmp_path, monkeypatch):
    """Someone editing this by hand will paste an absolute path.

    Prefixing home to it would build `/Users/me//Users/me/x`, which matches no
    `cwd` and reports nothing: the alias would just never fire.
    """
    write_config(tmp_path, monkeypatch, """
        [aliases]
        "/opt/old" = "/opt/new"
        "~/tilde-old" = "~/tilde-new"
    """)
    h = str(Path.home())
    assert load().aliases == {
        "/opt/old": "/opt/new",
        f"{h}/tilde-old": f"{h}/tilde-new",
    }


def test_the_two_tables_may_appear_in_either_order(tmp_path, monkeypatch):
    """Both live under a table header so their order cannot matter.

    A bare top-level `archived_paths` written after `[aliases]` would silently
    join that table instead — the exact mistake a hand-edited file makes.
    """
    write_config(tmp_path, monkeypatch, """
        [archived]
        paths = ["Documents/gone"]

        [aliases]
        "Documents/old" = "dev/new"
    """)
    cfg = load()
    assert cfg.archived_paths == (f"{Path.home()}/Documents/gone",)
    assert cfg.aliases == {f"{Path.home()}/Documents/old": f"{Path.home()}/dev/new"}


def test_a_malformed_config_file_names_itself_and_refuses_to_load(
    tmp_path, monkeypatch
):
    """Loud, because the silent version is unattributable.

    Absorbing the error drops every alias, and the symptom of *that* is one
    project splitting into several cards with nothing anywhere to say why.
    """
    p = write_config(tmp_path, monkeypatch, "[aliases\nbroken = ")
    with pytest.raises(ConfigError) as exc:
        load()
    assert str(p) in str(exc.value)


def test_a_non_table_aliases_key_is_refused(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, 'aliases = "not a table"\n')
    with pytest.raises(ConfigError):
        load()


def test_archived_paths_must_be_a_list(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, '[archived]\npaths = "Documents/gone"\n')
    with pytest.raises(ConfigError):
        load()


def test_overrides_still_beat_the_config_file(tmp_path, monkeypatch):
    """Every test that pins aliases inline depends on this ordering."""
    write_config(tmp_path, monkeypatch, """
        [aliases]
        "Documents/old" = "dev/new"
    """)
    assert load({"aliases": {}}).aliases == {}


# --- Phase 4 Task 1: the model catalog ---------------------------------------


def test_the_model_catalog_offers_pinned_versions_and_latest_aliases():
    """`value` reaches the wire, so it is what the assertions pin.

    An alias alone cannot express "pin me to 4.8": it floats to whatever is
    newest, which is the right default and the wrong record.
    """
    cfg = load()
    values = [m.value for m in cfg.models]
    assert "opus" in values           # latest-tracking alias
    assert "claude-opus-5" in values  # pinned
    assert "claude-opus-4-8" in values
    labels = {m.value: m.label for m in cfg.models}
    assert labels["claude-opus-4-8"] == "Opus 4.8"
    assert "Opus 5" in labels["opus"]  # the alias says what it currently means


def test_every_catalog_value_is_unique():
    values = [m.value for m in load().models]
    assert len(values) == len(set(values))


def test_the_catalog_default_is_an_alias_not_a_pin():
    """The first entry is what an unsuggested launch selects.

    Defaulting to a pinned version would quietly freeze every ad-hoc launch on
    a model that ages out, which is the opposite of what the alias is for.
    """
    assert load().models[0].value == "opus"


def test_a_model_choice_is_immutable():
    try:
        ModelChoice("opus", "Opus").value = "sonnet"
    except Exception:
        return
    raise AssertionError("ModelChoice must be frozen")


def test_stale_hours_is_read_from_the_config_file(tmp_path, monkeypatch):
    """How long a repo may sit dirty before the panel calls it stale is a
    judgement about how the user works, not a fact about anything."""
    write_config(tmp_path, monkeypatch, """
        [stale]
        hours = 3
    """)
    assert load().stale_hours == 3


def test_stale_hours_defaults_when_the_file_does_not_say(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, """
        [aliases]
        "Documents/old" = "dev/new"
    """)
    assert load().stale_hours == 12


def test_a_zero_or_negative_stale_hours_is_refused(tmp_path, monkeypatch):
    """It would mark every project stale the instant it went dirty, turning the
    one warning treatment into permanent furniture."""
    for value in ("0", "-1"):
        write_config(tmp_path, monkeypatch, f"[stale]\nhours = {value}\n")
        with pytest.raises(ConfigError):
            load()


def test_a_non_numeric_stale_hours_is_refused(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, '[stale]\nhours = "twelve"\n')
    with pytest.raises(ConfigError):
        load()


def test_an_override_still_beats_a_configured_stale_hours(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, "[stale]\nhours = 3\n")
    assert load({"stale_hours": 99}).stale_hours == 99


# --- Phase 7 Task 2: session-meta enrichment ---------------------------------


def test_session_meta_dir_defaults_under_claude_usage_data():
    cfg = load()
    assert cfg.session_meta_dir == Path.home() / ".claude" / "usage-data" / "session-meta"


def test_session_meta_dir_is_overridable(tmp_path):
    cfg = load({"session_meta_dir": tmp_path / "meta"})
    assert cfg.session_meta_dir == tmp_path / "meta"


# --- Distribution: [discovery] paths and the port key ------------------------


def test_discovery_paths_default_to_dev(monkeypatch):
    monkeypatch.delenv("BRIDGE_PORT", raising=False)
    assert load().discovery_paths == (Path.home() / "dev",)


def test_discovery_paths_are_read_from_the_config_file(tmp_path, monkeypatch):
    """A user with repos outside ~/dev names their roots here rather than in
    source, so `bridge setup` can configure discovery per machine."""
    write_config(tmp_path, monkeypatch, """
        [discovery]
        paths = ["dev", "/srv/work"]
    """)
    cfg = load()
    assert cfg.discovery_paths == (Path.home() / "dev", Path("/srv/work"))


def test_discovery_paths_must_be_a_list(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, '[discovery]\npaths = "dev"\n')
    with pytest.raises(ConfigError):
        load()


def test_port_is_read_from_the_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIDGE_PORT", raising=False)
    write_config(tmp_path, monkeypatch, "port = 8795\n")
    assert load().port == 8795


def test_an_out_of_range_port_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIDGE_PORT", raising=False)
    for value in ("0", "70000", '"8787"'):
        write_config(tmp_path, monkeypatch, f"port = {value}\n")
        with pytest.raises(ConfigError):
            load()


def test_bridge_port_env_wins_over_the_configured_port(tmp_path, monkeypatch):
    """The `port` field's own docstring promises this ordering: the file value
    is only the fallback the installer records, the env var is the deliberate
    per-run override. Regression — the file merge used to clobber it."""
    write_config(tmp_path, monkeypatch, "port = 9999\n")
    monkeypatch.setenv("BRIDGE_PORT", "5555")
    assert load().port == 5555


def test_an_override_still_beats_bridge_port(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, "port = 9999\n")
    monkeypatch.setenv("BRIDGE_PORT", "5555")
    assert load({"port": 7000}).port == 7000
