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

def _run(body: str, tmp_path):
    script = tmp_path / "case.js"
    script.write_text(
        f'const {{ makeDocument, load, report }} = require({json.dumps(str(MINIDOM))});\n'
        f'makeDocument(null);\n'
        f'load({json.dumps(str(STATIC / "morph.js"))});\n'
        f'{body}\n'
    )
    proc = subprocess.run([_node(), str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)

# Helper prelude available to every case body.
PRELUDE = """
function make(tag, attrs, text) {
  const el = document.createElement(tag);
  for (const k of Object.keys(attrs || {})) el.setAttribute(k, attrs[k]);
  if (text != null) el.textContent = text;
  return el;
}
"""

def test_updates_a_leaf_text_and_reports_the_change(tmp_path):
    got = _run(PRELUDE + """
        const live = make("div"); live.append(make("span", { id: "s" }, "old"));
        const inc  = make("div"); inc.append(make("span", { id: "s" }, "new"));
        const changed = [];
        window.bridgeMorph(live, inc, { onChange: (el) => changed.push(el.getAttribute("id")) });
        report({ text: live.children[0].textContent, changed });
    """, tmp_path)
    assert got["text"] == "new"
    assert got["changed"] == ["s"]

def test_syncs_attributes_adding_updating_and_removing(tmp_path):
    got = _run(PRELUDE + """
        const live = make("div", { id: "d", class: "a", stale: "1" });
        const inc  = make("div", { id: "d", class: "b" });
        window.bridgeMorph(live, inc, {});
        report({ cls: live.getAttribute("class"), stale: live.getAttribute("stale") });
    """, tmp_path)
    assert got["cls"] == "b"
    assert got["stale"] is None

def test_reuses_keyed_rows_and_reorders_without_rebuilding(tmp_path):
    got = _run(PRELUDE + """
        const live = make("ul");
        const a = make("li", { id: "a" }, "A"); const b = make("li", { id: "b" }, "B");
        live.append(a); live.append(b);
        const inc = make("ul");
        inc.append(make("li", { id: "b" }, "B")); inc.append(make("li", { id: "a" }, "A2"));
        window.bridgeMorph(live, inc, {});
        report({
          order: live.children.map((c) => c.getAttribute("id")),
          survivedB: live.children[0] === b,   // identity retained
          survivedA: live.children[1] === a,
          aText: live.children[1].textContent,
        });
    """, tmp_path)
    assert got["order"] == ["b", "a"]
    assert got["survivedB"] is True and got["survivedA"] is True
    assert got["aText"] == "A2"

def test_adds_new_rows_and_removes_dropped_rows(tmp_path):
    got = _run(PRELUDE + """
        const live = make("ul"); live.append(make("li", { id: "a" }, "A"));
        const inc = make("ul");
        inc.append(make("li", { id: "a" }, "A")); inc.append(make("li", { id: "c" }, "C"));
        window.bridgeMorph(live, inc, {});
        report({ order: live.children.map((c) => c.getAttribute("id")) });
    """, tmp_path)
    assert got["order"] == ["a", "c"]

def test_ignored_node_is_neither_morphed_nor_removed(tmp_path):
    got = _run(PRELUDE + """
        const live = make("form");
        const t = make("textarea", { id: "t" }, "user typing");
        t.setAttribute("data-live-preserve", "");
        live.append(t);
        const inc = make("form");
        inc.append(make("textarea", { id: "t" }, "SERVER VALUE"));  // would clobber
        const ignore = (el) => el.hasAttribute && el.hasAttribute("data-live-preserve");
        window.bridgeMorph(live, inc, { ignore });
        report({ kept: live.children.length === 1 && live.children[0] === t,
                 text: live.children[0].textContent });
    """, tmp_path)
    assert got["kept"] is True
    assert got["text"] == "user typing", "an ignored node must not be morphed"
