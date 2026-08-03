"""Settings read model: the `/settings` page's effective configuration.

The plan's signature named this function's optional parameter `hook_state`,
but that name already belongs to `bridge.hooks.HookState` -- an in-memory,
unpersisted `needs_input` overlay fed by `POST /api/hooks`, which answers "is
a session waiting on a human right now" and has nothing to do with what this
page shows. What Settings reports is whether Claude Code's own
`~/.claude/settings.json` still has Bridge's three hooks (`Notification`,
`SessionStart`, `SessionEnd`) wired to `http://127.0.0.1:{port}/api/hooks` --
a fact on disk, not a fact in this process. `settings_path` is the seam that
keeps that read out of the developer's real file in every test.

Everything else here is a read straight off an already-loaded `Config`: no
secrets exist on `Config` today, and V1 keeps machine configuration
read-only, so there is no write path anywhere in this module (spec line 261).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from bridge.config import Config, ModelChoice, PermissionChoice, config_path

# The only three events Bridge itself installs (recon §3b). A real
# settings.json also carries unrelated events (UserPromptSubmit, PreToolUse,
# PostToolUse, Stop) that must never be mistaken for these.
HOOK_EVENTS = ("Notification", "SessionStart", "SessionEnd")


@dataclass(frozen=True)
class HookEventStatus:
    """Whether one of the three Bridge hook events is correctly wired."""

    name: str
    installed: bool


@dataclass(frozen=True)
class HookStatus:
    """Overall verdict plus per-event detail and recovery guidance.

    `issues` is empty exactly when `state == "present"` -- the same "quiet
    when healthy" posture `_attention_items()` (api.py:686-712) uses for
    Diagnostics, so Settings does not editorialize about a healthy install.
    """

    state: str  # "present" | "partial" | "absent"
    events: tuple[HookEventStatus, ...]
    issues: tuple[dict, ...] = ()


@dataclass(frozen=True)
class LaunchDefaults:
    """The launch-band catalogs Task 5.2's safe-launch-default selectors
    populate from. Pass-through copies of `Config`'s lists: `permission_modes`
    keeps whatever order `Config` declares, which puts "Ask as usual" (the
    empty, no-flag value) first."""

    models: list[ModelChoice]
    efforts: list[str]
    permission_modes: list[PermissionChoice]


@dataclass(frozen=True)
class SettingsModel:
    """The `/settings` page's full state: effective config, hook status, and
    launch-default catalogs. No `Store` dependency -- this is pure filesystem
    (one settings.json read) plus `Config` attribute reads, mirroring
    `build_projects`/`build_schedule`'s read-model shape."""

    config_path: Path
    claude_projects_dir: Path
    session_meta_dir: Path
    stale_hours: int
    aliases: dict[str, str]
    archived_paths: tuple[str, ...]
    db_path: Path
    port: int
    hook_status: HookStatus
    launch_defaults: LaunchDefaults


def build_settings(cfg: Config, *, settings_path: Path | None = None) -> SettingsModel:
    """Assemble the Settings page.

    `settings_path` exists purely for test determinism (mirroring
    `probe_fn`/`agents_fn` on `build_projects`): when omitted it defaults to
    the real `~/.claude/settings.json`, read once, for status only, and never
    written. `cfg` is read, never reloaded -- the caller's already-loaded
    `Config` is the one source of truth for `port`, exactly as every other
    `build_*` takes its `Config` as a parameter rather than calling `load()`
    itself.
    """
    if settings_path is None:
        settings_path = Path.home() / ".claude" / "settings.json"

    return SettingsModel(
        config_path=config_path(),
        claude_projects_dir=cfg.claude_projects_dir,
        session_meta_dir=cfg.session_meta_dir,
        stale_hours=cfg.stale_hours,
        aliases=dict(cfg.aliases),
        archived_paths=tuple(cfg.archived_paths),
        db_path=cfg.db_path,
        port=cfg.port,
        hook_status=_hook_status(cfg, settings_path),
        launch_defaults=LaunchDefaults(
            models=list(cfg.models),
            efforts=list(cfg.efforts),
            permission_modes=list(cfg.permission_modes),
        ),
    )


def _hook_status(cfg: Config, settings_path: Path) -> HookStatus:
    """Read `settings_path` (never written) and decide present/partial/absent.

    A missing file, unreadable file, or malformed JSON all fall through to an
    empty `hooks` dict rather than raising -- the fresh-install case must
    answer "absent + how to install", never a 500 (brief's no-crash
    requirement; mirrors `_read_config_file`'s "absent file sets none" and
    `hooks.HookState.record()`'s tolerant posture).
    """
    expected_url = f"http://127.0.0.1:{cfg.port}/api/hooks"

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}

    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        hooks = {}

    # The three http handlers are only half the wiring: Claude Code will not
    # actually call an http hook whose URL is not in `allowedHttpHookUrls`, and
    # `_hook_issue.next_action` already tells users to add it. So "present"
    # requires BOTH the handlers AND the endpoint being allow-listed; otherwise
    # the same recovery guidance applies, as partial or absent.
    allowed = data.get("allowedHttpHookUrls") if isinstance(data, dict) else None
    url_allowed = isinstance(allowed, list) and expected_url in allowed

    events = tuple(
        HookEventStatus(name=name, installed=_event_installed(hooks.get(name), expected_url))
        for name in HOOK_EVENTS
    )

    installed_count = sum(1 for e in events if e.installed)
    if installed_count == len(HOOK_EVENTS) and url_allowed:
        state = "present"
    elif installed_count == 0:
        state = "absent"
    else:
        state = "partial"

    issues = () if state == "present" else (_hook_issue(state, events, expected_url),)
    return HookStatus(state=state, events=events, issues=issues)


def _event_installed(entry: object, expected_url: str) -> bool:
    """One event is installed when it has an `http` handler pointed at this
    port's `/api/hooks` -- the exact shape Task 9 of the phase-4 amendments
    wrote (recon §3b): `{"hooks": [{"type": "http", "url": ..., "timeout": 2}]}`.
    """
    if not isinstance(entry, dict):
        return False
    handlers = entry.get("hooks")
    if not isinstance(handlers, list):
        return False
    return any(
        isinstance(h, dict) and h.get("type") == "http" and h.get("url") == expected_url
        for h in handlers
    )


def _hook_issue(state: str, events: tuple[HookEventStatus, ...], expected_url: str) -> dict:
    """One cause/next_action dict, in the shape `_attention_items()`
    (api.py:686-712) already uses for Diagnostics -- the one existing
    precedent in this codebase for "explain what's wrong and what to do
    about it".
    """
    missing = [e.name for e in events if not e.installed]
    if state == "absent":
        cause = (
            "Bridge's Claude Code hooks are not installed in "
            "~/.claude/settings.json (or point at a different port)."
        )
    else:
        cause = (
            f"{', '.join(missing)} hook(s) are missing from "
            "~/.claude/settings.json, or point at a different port."
        )
    handler_shape = (
        '{"hooks": [{"type": "http", "url": "' + expected_url + '", "timeout": 2}]}'
    )
    return {
        "label": "Claude Code hooks not fully installed",
        "cause": cause,
        "next_action": (
            "Add Notification, SessionStart, and SessionEnd entries under "
            '"hooks" in ~/.claude/settings.json, each shaped '
            f"{handler_shape}, and add that same URL to "
            '"allowedHttpHookUrls".'
        ),
    }
