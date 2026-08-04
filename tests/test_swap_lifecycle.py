"""Multi-document-lifetime tests: what still works after the document is swapped.

The rest of the suite is structurally blind to this. Every route test renders one
full document and every test in `test_static_js.py` models exactly one document
lifetime, so a swap lifecycle would otherwise ship with no regression net at all.

These run the REAL static files under node against a hand-rolled DOM
(`tests/js/minidom.js`). That DOM is deliberately small: it models element
identity, attribute selectors and event bubbling, which is all the swap contract
turns on. It does NOT model layout, CSS, or true browser event semantics -- see
the module docstring in minidom.js for the exact boundary.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "bridge" / "static"
MINIDOM = Path(__file__).resolve().parent / "js" / "minidom.js"

NODE_CANDIDATES = (
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
    "/usr/bin/node",
)


def _node() -> str | None:
    # `tools/falsify.py` runs pytest with PATH=/usr/bin:/bin, where Homebrew's
    # node is invisible. A bare `shutil.which` therefore SKIPS this module under
    # falsification, pytest exits 0, and every mutation comes back SURVIVED --
    # a skipped test is indistinguishable from a passing one. Same reasoning and
    # same list as tests/test_static_js.py:32.
    found = shutil.which("node")
    if found:
        return found
    return next((p for p in NODE_CANDIDATES if Path(p).exists()), None)


def run_js(body: str, files: list[str], tmp_path) -> dict:
    """Load `files` from static/ in order into a mini-DOM realm, then run `body`."""
    script = tmp_path / "case.js"
    loads = "\n".join(
        f'load({json.dumps(str(STATIC / name))});' for name in files
    )
    script.write_text(
        f'const {{ makeDocument, load, report }} = require({json.dumps(str(MINIDOM))});\n'
        f'{loads}\n{body}\n'
    )
    proc = subprocess.run([_node(), str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


pytestmark = pytest.mark.skipif(_node() is None, reason="node is not installed")


def test_registry_runs_enter_hooks_in_registration_order(tmp_path):
    got = run_js(
        """
        const seen = [];
        window.bridgePage.onEnter(() => seen.push("a"));
        window.bridgePage.onEnter(() => seen.push("b"));
        window.bridgePage.enter();
        report({ seen });
        """,
        ["shell.js"],
        tmp_path,
    )
    assert got["seen"] == ["a", "b"]


def test_one_bad_enter_hook_does_not_abort_the_others(tmp_path):
    got = run_js(
        """
        const seen = [];
        window.bridgePage.onEnter(() => { throw new Error("boom"); });
        window.bridgePage.onEnter(() => seen.push("survived"));
        window.bridgePage.enter();
        report({ seen });
        """,
        ["shell.js"],
        tmp_path,
    )
    assert got["seen"] == ["survived"], (
        "one page's broken enter hook took down every other page's"
    )


def test_shell_js_fires_the_initial_enter_hook_once_on_first_load(tmp_path):
    """The registry only QUEUES hooks -- nothing else performs the FIRST page
    view. router.js (task 9) calls `enter()` only after a swap, so without a
    bootstrap here every onEnter hook registered by an ordinary page load
    would sit in the queue forever, unfired. Every static file is `defer`, so
    all of them register their hooks before `DOMContentLoaded` fires -- that
    event is shell.js's own trigger for the first view. Dispatching it twice
    proves the boot itself is idempotent, not merely that the listener was
    added once."""
    script = tmp_path / "case.js"
    shell_path = STATIC / "shell.js"
    script.write_text(
        f'const {{ makeDocument, report }} = require({json.dumps(str(MINIDOM))});\n'
        f'const doc = makeDocument(null);\n'
        f'doc.readyState = "loading";\n'
        f'require("vm").runInThisContext(\n'
        f'  require("fs").readFileSync({json.dumps(str(shell_path))}, "utf8"),\n'
        f'  {{ filename: {json.dumps(str(shell_path))} }}\n'
        f');\n'
        f'const seen = [];\n'
        f'window.bridgePage.onEnter(() => seen.push("entered"));\n'
        f'doc.dispatchEvent({{ type: "DOMContentLoaded" }});\n'
        f'doc.dispatchEvent({{ type: "DOMContentLoaded" }});\n'
        f'report({{ seen }});\n'
    )
    proc = subprocess.run([_node(), str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["seen"] == ["entered"], (
        "shell.js must fire the first view's onEnter hooks exactly once off "
        "DOMContentLoaded, even if that event were somehow dispatched again"
    )


def test_leave_hooks_run_on_leave_and_not_on_enter(tmp_path):
    got = run_js(
        """
        const seen = [];
        window.bridgePage.onLeave(() => seen.push("left"));
        window.bridgePage.enter();
        const afterEnter = seen.slice();
        window.bridgePage.leave();
        report({ afterEnter, afterLeave: seen });
        """,
        ["shell.js"],
        tmp_path,
    )
    assert got["afterEnter"] == []
    assert got["afterLeave"] == ["left"]
