import json, shutil, subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "bridge" / "static"
MINIDOM = Path(__file__).resolve().parent / "js" / "minidom.js"
NODE_CANDIDATES = ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node")

def _node():
    found = shutil.which("node")
    return found or next((p for p in NODE_CANDIDATES if Path(p).exists()), None)

pytestmark = pytest.mark.skipif(_node() is None, reason="node is not installed")

def _run(body: str, files, tmp_path):
    script = tmp_path / "case.js"
    loads = "\n".join(f'load({json.dumps(str(STATIC / n))});' for n in files)
    script.write_text(
        f'const {{ makeDocument, load, report }} = require({json.dumps(str(MINIDOM))});\n'
        f'makeDocument(null);\n{loads}\n{body}\n'
    )
    proc = subprocess.run([_node(), str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)

def test_registered_listeners_receive_emitted_frames(tmp_path):
    got = _run(
        """
        const seen = [];
        window.bridgeLive.onFrame((f) => seen.push(f.generation));
        window.bridgeLive._emit({ generation: 42 });
        report({ seen });
        """,
        ["shell.js", "live.js"],
        tmp_path,
    )
    assert got["seen"] == [42]

def test_one_throwing_listener_does_not_stop_the_others(tmp_path):
    got = _run(
        """
        const seen = [];
        window.bridgeLive.onFrame(() => { throw new Error("boom"); });
        window.bridgeLive.onFrame((f) => seen.push(f.generation));
        window.bridgeLive._emit({ generation: 7 });
        report({ seen });
        """,
        ["shell.js", "live.js"],
        tmp_path,
    )
    assert got["seen"] == [7]
