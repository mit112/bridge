import json
from pathlib import Path

import pytest

REAL_BRIDGE_DIR = Path.home() / ".bridge"


def jline(**kw) -> str:
    return json.dumps(kw) + "\n"


@pytest.fixture(autouse=True)
def never_touch_the_real_bridge_dir(monkeypatch):
    """Refuse any spool operation against the user's real `~/.bridge`.

    `create_app` drains the spool on boot, and a drain MOVES files out of it. A
    fixture that overrides `db_path` but forgets `spool_dir` would therefore
    quietly consume real, unrecoverable handoffs — the one kind of data in
    Bridge that cannot be rebuilt from transcripts. Guarding here is what makes
    that a loud failure instead of a silent one, rather than trusting every
    present and future fixture to remember.
    """
    from bridge import spool

    def guarded(name, orig):
        def wrapper(*args, **kwargs):
            for value in (*args, *kwargs.values()):
                if isinstance(value, (str, Path)):
                    p = Path(value)
                    if p == REAL_BRIDGE_DIR or REAL_BRIDGE_DIR in p.parents:
                        raise AssertionError(
                            f"spool.{name}() was called with the real path {p}. "
                            "Pass spool_dir=tmp_path/'spool' in this test's Config."
                        )
            return orig(*args, **kwargs)

        return wrapper

    for name in ("write", "journal", "drain", "rebuild_if_empty", "pending",
                 "pending_count"):
        monkeypatch.setattr(spool, name, guarded(name, getattr(spool, name)))


@pytest.fixture
def write_transcript(tmp_path):
    """Write JSONL lines to a file and return its path."""

    def _write(name: str, lines: list[str]) -> Path:
        p = tmp_path / name
        p.write_text("".join(lines))
        return p

    return _write


@pytest.fixture
def normal_session():
    """A realistic minimal session: title, two turns, usage, cwd, branch."""
    sid = "11111111-1111-1111-1111-111111111111"
    return sid, [
        jline(type="last-prompt", leafUuid="a", sessionId=sid),
        jline(
            type="user", sessionId=sid, isSidechain=False,
            timestamp="2026-07-30T10:00:00.000Z",
            cwd="/Users/mitsheth/dev/demo", gitBranch="main",
            message={"role": "user", "content": "do the thing"},
        ),
        jline(
            type="assistant", sessionId=sid, isSidechain=False,
            timestamp="2026-07-30T10:00:05.000Z",
            cwd="/Users/mitsheth/dev/demo", gitBranch="main", effort="high",
            message={
                "role": "assistant", "model": "claude-opus-5",
                "usage": {
                    "input_tokens": 10, "output_tokens": 20,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 40,
                },
            },
        ),
        jline(type="ai-title", sessionId=sid, aiTitle="Do the thing"),
        jline(type="last-prompt", sessionId=sid, lastPrompt="do the thing again"),
    ]
