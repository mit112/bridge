"""Configuration for Bridge.

`aliases` and `archived_paths` name directories on one particular machine, so
they live in `~/.bridge/config.toml` and not in version-controlled source:
adding an alias should not mean editing Python. Overrides are for tests.

The model catalog, the effort list and the permission modes stay constants here
because they are facts about the `claude` CLI rather than about the user.

`stale_hours` is there too (spec line 366): how long a repo may sit dirty before
the panel calls it stale is a judgement about how the user works, not a fact
about anything.

Known divergence, recorded rather than silently kept: spec lines 375-378 also
put `models` and `efforts` in the file, seeded on first run. They have not
moved, deliberately -- see the note above the catalog.
"""

import os
import tomllib
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
# than left implicit. Spec line 377 puts this list in `config.toml`; it has not
# moved, and the module docstring records that as an open decision.
DEFAULT_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


class ConfigError(Exception):
    """A malformed `config.toml`, reported with the file that caused it.

    Absorbing the error instead would drop every alias, and the symptom of that
    is one project silently splitting into several cards with nothing anywhere
    to say why. A hand-edited file has to fail loudly.
    """


def config_path() -> Path:
    """Where `load` looks for the config file.

    `BRIDGE_CONFIG` exists for the same reason `BRIDGE_PORT` does: so the suite
    can be pointed somewhere hermetic. Without it every test would inherit
    whatever aliases the developer happens to have declared.
    """
    return Path(
        os.environ.get("BRIDGE_CONFIG") or Path.home() / ".bridge" / "config.toml"
    )


def _absolute(path: str) -> str:
    """Home-relative by default, because that keeps the file portable.

    An absolute path is honoured as written. Someone editing this file by hand
    will paste one, and prefixing home to it would produce a path that matches
    no transcript `cwd` and reports no error -- the alias would simply never
    fire. Absolute is the form a `cwd` takes, so that is what this returns.
    """
    p = Path(path).expanduser()
    return str(p if p.is_absolute() else Path.home() / p)


def _read_config_file(path: Path) -> dict:
    """The fields this file may set, as `replace` kwargs. Absent file sets none.

    Only keys actually present are returned, so precedence in `load` reads as
    defaults < file < overrides with no sentinel values to interpret.

    Every setting lives under a table header so its order in the file cannot
    matter. A bare top-level `archived_paths` written *after* `[aliases]` would
    silently become a member of that table instead, which is precisely the kind
    of mistake a hand-edited file makes and nothing would report.
    """
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    aliases = data.get("aliases", {})
    archived = data.get("archived", {})
    stale = data.get("stale", {})
    if not all(isinstance(t, dict) for t in (aliases, archived, stale)):
        raise ConfigError(
            f"{path}: [aliases], [archived] and [stale] must be tables"
        )
    paths = archived.get("paths", [])
    if not isinstance(paths, list):
        raise ConfigError(f"{path}: archived.paths must be a list of paths")

    values: dict = {}
    if aliases:
        values["aliases"] = {_absolute(a): _absolute(c) for a, c in aliases.items()}
    if paths:
        values["archived_paths"] = tuple(_absolute(p) for p in paths)
    if "hours" in stale:
        hours = stale["hours"]
        # Zero or negative would mark every project stale the instant it went
        # dirty, turning the one warning treatment into permanent furniture.
        if not isinstance(hours, int) or hours < 1:
            raise ConfigError(f"{path}: stale.hours must be a positive whole number")
        values["stale_hours"] = hours
    discovery = data.get("discovery", {})
    if not isinstance(discovery, dict):
        raise ConfigError(f"{path}: [discovery] must be a table")
    disc_paths = discovery.get("paths", [])
    if not isinstance(disc_paths, list):
        raise ConfigError(f"{path}: discovery.paths must be a list of paths")
    if disc_paths:
        values["discovery_paths"] = tuple(
            Path(_absolute(p)) for p in disc_paths
        )
    # Port from config file (env var BRIDGE_PORT takes precedence in load()).
    if "port" in data:
        port = data["port"]
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise ConfigError(f"{path}: port must be an integer between 1 and 65535")
        values["port"] = port
    return values


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
    session_meta_dir: Path
    db_path: Path
    spool_dir: Path
    # Where a launch's prompt file is written, mode 0700. The prompt is taken off
    # the command line entirely, so this directory is the only place it exists
    # between `launch()` and the new shell's `cat`.
    launches_dir: Path
    # Directories to scan one level deep for git repos not yet indexed.
    # Defaults to (~/dev,).  Configured via [discovery] paths in config.toml.
    discovery_paths: tuple[Path, ...]
    stale_hours: int
    models: list[ModelChoice]
    efforts: list[str]
    permission_modes: list[PermissionChoice]
    port: int
    # Both from `config.toml`, absolute. Empty when there is no file: these name
    # one machine's directories, so having none is the ordinary fresh-install
    # state and not a degraded one.
    aliases: dict[str, str]
    archived_paths: tuple[str, ...]


def load(overrides: dict | None = None) -> Config:
    home = Path.home()
    cfg = Config(
        claude_projects_dir=home / ".claude" / "projects",
        session_meta_dir=home / ".claude" / "usage-data" / "session-meta",
        db_path=home / ".bridge" / "bridge.db",
        spool_dir=home / ".bridge" / "spool",
        launches_dir=home / ".bridge" / "launches",
        discovery_paths=(home / "dev",),
        stale_hours=12,
        models=list(DEFAULT_MODELS),
        efforts=list(DEFAULT_EFFORTS),
        permission_modes=list(DEFAULT_PERMISSION_MODES),
        # Env-overridable so the CLI's exit-zero-when-the-panel-is-down property
        # can be tested in a real subprocess against a genuinely closed port,
        # rather than against a mocked transport. BRIDGE_PORT wins over config.toml.
        port=int(os.environ.get("BRIDGE_PORT") or 8787),
        aliases={},
        archived_paths=(),
    )
    # defaults < config.toml < BRIDGE_PORT < overrides. The file can only reach
    # the fields it actually names, and a test's explicit override always wins.
    cfg = replace(cfg, **_read_config_file(config_path()))
    # BRIDGE_PORT wins over config.toml's `port` (see the `port` field's note):
    # the file value is only the fallback the installer records so
    # `--launchd-only` can recover it, whereas the env var is the deliberate
    # per-run override. Re-applied here because the file merge above would
    # otherwise clobber the env value set at construction.
    env_port = os.environ.get("BRIDGE_PORT")
    if env_port:
        cfg = replace(cfg, port=int(env_port))
    if overrides:
        cfg = replace(cfg, **overrides)
    return cfg
