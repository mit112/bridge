import json, shutil, subprocess
from pathlib import Path
import pytest

MINIDOM = Path(__file__).resolve().parent / "js" / "minidom.js"
NODE_CANDIDATES = ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node")

def _node():
    found = shutil.which("node")
    if found:
        return found
    return next((p for p in NODE_CANDIDATES if Path(p).exists()), None)

pytestmark = pytest.mark.skipif(_node() is None, reason="node is not installed")

def _run(body: str, tmp_path):
    script = tmp_path / "case.js"
    script.write_text(
        f'const {{ El, makeDocument, report }} = require({json.dumps(str(MINIDOM))});\n'
        f'makeDocument(null);\n{body}\n'
    )
    proc = subprocess.run([_node(), str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)

def test_get_attribute_names_lists_every_set_attribute(tmp_path):
    got = _run(
        """
        const el = document.createElement("div");
        el.setAttribute("id", "x");
        el.setAttribute("data-key", "7");
        report({ names: el.getAttributeNames().sort() });
        """,
        tmp_path,
    )
    assert got["names"] == ["data-key", "id"]

def test_insert_before_moves_an_existing_child_without_duplicating(tmp_path):
    got = _run(
        """
        const box = document.createElement("ul");
        const a = document.createElement("li"); a.setAttribute("id", "a");
        const b = document.createElement("li"); b.setAttribute("id", "b");
        box.append(a); box.append(b);
        box.insertBefore(b, a);              // move b before a
        report({ order: box.children.map((c) => c.getAttribute("id")) });
        """,
        tmp_path,
    )
    assert got["order"] == ["b", "a"], "insertBefore must move, not duplicate"
