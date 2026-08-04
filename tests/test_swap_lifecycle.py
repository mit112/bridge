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
    would sit in the queue forever, unfired.

    The ORDER below is the whole point, not incidental: `run_js` loads
    shell.js FIRST, exactly as `base.html` does (it is the first `<script
    defer>`), and only after that does this test register an `onEnter` hook --
    exactly the sequence every other static file follows on a real page. A
    version of this test that registered the hook BEFORE shell.js ran could
    not tell a correct implementation from a broken one: a broken shell.js
    that eagerly calls `enter()` during its own evaluation (instead of
    waiting for `DOMContentLoaded`) would find that hook already queued and
    "coincidentally" pass.

    `DOMContentLoaded` fires only once every deferred script (this one
    included) has finished executing -- readiness is already "interactive"
    the whole time a `<script defer>` runs, never "loading" (see minidom.js's
    `makeDocument`) -- so shell.js must listen for it unconditionally rather
    than branching on `readyState`, which is exactly the bug this test
    guards against."""
    got = run_js(
        """
        const seen = [];
        window.bridgePage.onEnter(() => seen.push("entered"));
        document.dispatchEvent({ type: "DOMContentLoaded" });
        report({ seen });
        """,
        ["shell.js"],
        tmp_path,
    )
    assert got["seen"] == ["entered"], (
        "shell.js must fire the first view's onEnter hooks off "
        "DOMContentLoaded -- registered unconditionally, not gated on "
        "readyState, which a deferred script never observes as \"loading\""
    )


def test_scheduled_times_are_repainted_on_every_page_view(tmp_path):
    """schedule.js bound this to DOMContentLoaded, which fires once per document.

    The server stores epoch seconds and only the browser knows the viewer's
    timezone, so a cell that is never repainted shows a raw UTC string forever.
    """
    got = run_js(
        """
        const { El } = require(MINIDOM);
        const cell = new El("span", { "data-scheduled-for": "1754300000" });
        document.body.append(cell);
        const beforeEnter = cell.textContent;
        window.bridgePage.enter();
        const afterFirst = cell.textContent;

        // A second page view with a fresh cell -- what a swap actually produces.
        const swapped = new El("span", { "data-scheduled-for": "1754300000" });
        cell.remove();
        document.body.append(swapped);
        window.bridgePage.enter();
        report({ beforeEnter, afterFirst, afterSwap: swapped.textContent });
        """.replace("MINIDOM", json.dumps(str(MINIDOM))),
        ["shell.js", "schedule.js"],
        tmp_path,
    )
    assert got["beforeEnter"] == ""
    assert got["afterFirst"] != ""
    assert got["afterSwap"] != "", (
        "a scheduled time rendered after a swap was never converted to local "
        "time -- the viewer sees the server's raw UTC value"
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
