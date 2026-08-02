import json
from pathlib import Path

import pytest

REAL_BRIDGE_DIR = Path.home() / ".bridge"


class RealBridgeDirTouched(BaseException):
    """Deliberately not an `Exception`.

    The boot drain is wrapped in a broad `except` so a broken spool cannot stop
    the panel from starting, and that swallowed this guard when it raised
    `AssertionError` — the guard reported nothing while the test drained the real
    spool. Inheriting from `BaseException` puts it out of reach of any
    well-behaved catch-all.
    """


def jline(**kw) -> str:
    return json.dumps(kw) + "\n"


def launch_by_session(store, session_id: str):
    """The launch-to-session join, read straight from the table.

    `Store` carries no query for this: nothing the panel renders looks a launch
    up by session id, so the method that used to live here was dead code. The
    correlation itself is the thing Phase 3 is most careful about, so the tests
    that assert it do the SELECT here rather than infer the answer from a row's
    position in `store.launches()` -- a launch that linked to the WRONG session
    would still be in position 0.
    """
    return store.conn.execute(
        "SELECT * FROM launches WHERE session_id=?", (session_id,)
    ).fetchone()


@pytest.fixture(autouse=True)
def never_touch_the_real_bridge_dir(monkeypatch):
    """Refuse any spool or launcher operation against the user's real `~/.bridge`.

    `create_app` drains the spool on boot, and a drain MOVES files out of it. A
    fixture that overrides `db_path` but forgets `spool_dir` would therefore
    quietly consume real, unrecoverable handoffs — the one kind of data in
    Bridge that cannot be rebuilt from transcripts. Guarding here is what makes
    that a loud failure instead of a silent one, rather than trusting every
    present and future fixture to remember.

    Phase 3 adds `~/.bridge/launches`, and a launch WRITES there, so the same
    guard covers the launcher's directory-taking entry points. Both modules are
    guarded by one fixture on purpose: a second autouse guard would be one more
    thing to forget to extend.
    """
    from bridge import launcher, schedspool, spool

    def guarded(module_name, name, orig, override):
        def wrapper(*args, **kwargs):
            for value in (*args, *kwargs.values()):
                if isinstance(value, (str, Path)):
                    p = Path(value)
                    if p == REAL_BRIDGE_DIR or REAL_BRIDGE_DIR in p.parents:
                        raise RealBridgeDirTouched(
                            f"{module_name}.{name}() was called with the real path "
                            f"{p}. Pass {override}=tmp_path/'{override.split('_')[0]}'"
                            " in this test's Config."
                        )
            return orig(*args, **kwargs)

        return wrapper

    # Every spool entry point that takes a directory belongs here. `journal_status`
    # is Phase 3's and was added to this tuple with it: a writer that is missing
    # from the list is not guarded at all, and the omission is invisible until a
    # test has already written to the real spool.
    for name in ("write", "journal", "journal_status", "drain", "rebuild_if_empty",
                 "pending", "pending_count"):
        monkeypatch.setattr(spool, name,
                            guarded("spool", name, getattr(spool, name), "spool_dir"))

    # Same contract for the scheduled-run journal. `rebuild_if_empty` does not
    # exist yet -- it lands in the replay task -- and this tuple must grow to
    # include it then, the same way `journal_status` grew this block above.
    for name in ("journal", "journal_status"):
        monkeypatch.setattr(
            schedspool, name,
            guarded("schedspool", name, getattr(schedspool, name), "spool_dir"),
        )

    # The launcher's writers take `launches_dir` rather than a `Config` precisely
    # so this guard can see the path: an argument-inspecting wrapper cannot look
    # inside a dataclass. `launch()` calls both through the module global, so
    # patching here intercepts the internal calls too.
    for name in ("write_prompt_file", "gc_prompt_files"):
        monkeypatch.setattr(
            launcher, name,
            guarded("launcher", name, getattr(launcher, name), "launches_dir"),
        )


@pytest.fixture(autouse=True)
def never_read_the_real_config_file(tmp_path, monkeypatch):
    """Point `config.load` at a file that does not exist, for every test.

    The guard above cannot cover this one: it inspects the arguments a function
    was called with, and reading `~/.bridge/config.toml` is a bare read inside
    `load()` that takes no path at all. Without this fixture every one of the
    ~90 `load(...)` calls in the suite would inherit whichever aliases the
    developer happens to have declared -- an alias whose key matched a fixture's
    `cwd` would quietly change what a test indexes, and the suite would pass or
    fail depending on the machine. Tests that want a config file set
    `BRIDGE_CONFIG` themselves.
    """
    monkeypatch.setenv("BRIDGE_CONFIG", str(tmp_path / "no-such-config.toml"))


@pytest.fixture(autouse=True)
def never_read_the_real_session_registry(tmp_path, monkeypatch):
    """Point the liveness sensor at an empty directory for every test.

    `agents.probe` defaults to `~/.claude/sessions`, so without this the suite
    reads whatever Claude sessions happen to be running on the developer's
    machine: results would differ between a laptop with three sessions open and
    CI with none, and a card could gain a live band nobody put there.

    An empty *existing* directory, not a missing one, because those mean
    different things -- missing is `unavailable`, empty is "nothing running" --
    and "nothing running" is the neutral default a test should start from.
    Tests that care pass `agents_fn` explicitly.
    """
    from bridge import agents

    empty = tmp_path / "empty-sessions"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(agents, "SESSIONS_DIR", empty)


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
