"""The one piece of JavaScript behaviour that a Python suite cannot infer.

`copy.js` used to read `source.textContent`. On a `<pre>` that is the prompt; on
the `<textarea>` Phase 3 replaces it with, it is the **server-rendered** text and
not what the user typed, so Copy would hand over a stale prompt while looking
like it worked. The plan names this specifically, and everything else in Phase 3
that could go wrong has a test, so this should too.

The rest of the front end is asserted structurally from the rendered HTML in
`test_api.py`. This module exists only because `bridgeText`'s contract is a
property of the DOM object it is handed, which no amount of HTML inspection
reveals. It runs the real file under `node` with a minimal stub rather than
adding a JS test framework, a bundler, or a dependency.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

COPY_JS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "copy.js"

# `tools/falsify.py` runs pytest with `PATH=/usr/bin:/bin`, and Homebrew's node is
# on neither. A bare `shutil.which("node")` therefore skipped this module under
# falsification, pytest exited 0, and the mutation that reverts the `.value` fix
# came back SURVIVED — a skipped test is indistinguishable from a passing one.
# Searching the known absolute locations as well is what makes the mutation real,
# and is the same reason the rest of this repo shells out to absolute paths.
NODE_CANDIDATES = (
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
    "/usr/bin/node",
)


def _node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    return next((p for p in NODE_CANDIDATES if Path(p).exists()), None)

# `copy.js` registers a delegated listener and assigns onto `window` at load, so
# both have to exist before it is evaluated. Nothing here simulates a real DOM:
# the assertion is only about which property `bridgeText` prefers.
HARNESS = """
globalThis.window = globalThis;
globalThis.document = { addEventListener() {}, getElementById: () => null,
                        querySelector: () => null, createRange: () => ({}) };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));
console.log(JSON.stringify({
  // A form control: `.value` is the live, user-edited text.
  textarea: window.bridgeText({ value: "EDITED", textContent: "RENDERED" }),
  // A <pre>: no `.value`, so `.textContent` is still correct.
  pre: window.bridgeText({ textContent: "RENDERED" }),
  // An empty edit must not silently fall back to the rendered text.
  cleared: window.bridgeText({ value: "", textContent: "RENDERED" }),
}));
"""


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_bridge_text_prefers_the_live_value_over_the_rendered_text(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)

    proc = subprocess.run(
        [_node(), str(harness), str(COPY_JS)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)

    assert got["textarea"] == "EDITED", (
        "Copy handed over the server-rendered prompt instead of the user's edit"
    )
    assert got["pre"] == "RENDERED"
    assert got["cleared"] == "", (
        "an emptied textarea fell back to the rendered text, so Copy would "
        "silently resurrect a prompt the user had deleted"
    )
