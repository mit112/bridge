import json
from pathlib import Path

import pytest


def jline(**kw) -> str:
    return json.dumps(kw) + "\n"


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
