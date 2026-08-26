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

# --- Codex review finding #7: mixed content and branch-to-leaf updates ------
#
# The old algorithm reconciled `.children` (elements only), so any direct
# text node sharing a parent with an element -- `<p>Handoff saved
# <time>...</time></p>` in `_workspace_current.html`, `<p><strong>Next:
# </strong> ...</p>` in `diagnostics.html` -- was invisible to it. Cloning a
# brand-new such node dropped the leading text; morphing an EXISTING one
# never touched trailing text at all, since only the element sibling
# (`<time>`/`<strong>`) got reconciled.


def test_a_freshly_cloned_mixed_content_node_keeps_its_leading_text(tmp_path):
    """The create path: this parent does not exist in `live` yet, so it goes
    through `cloneInto`, not `morphNode`."""
    got = _run(PRELUDE + """
        const live = make("div");
        const inc = make("div");
        const p = document.createElement("p");
        p.append(document.createTextNode("Handoff saved"));
        p.append(make("time", {}, "3m ago"));
        inc.append(p);
        window.bridgeMorph(live, inc, {});
        report({ text: live.children[0].textContent });
    """, tmp_path)
    assert got["text"] == "Handoff saved3m ago"


def test_morphing_an_existing_mixed_content_node_updates_trailing_text(tmp_path):
    """The update path: `<strong>Next:</strong> old` already exists in `live`;
    only the text AFTER the element changes. The old code never inspected
    that text node at all, so it stayed stale forever."""
    got = _run(PRELUDE + """
        function mixedNode(trailing) {
          const p = document.createElement("p");
          p.append(make("strong", {}, "Next:"));
          p.append(document.createTextNode(" " + trailing));
          return p;
        }
        const live = make("div"); live.append(mixedNode("old action"));
        const inc = make("div"); inc.append(mixedNode("new action"));
        window.bridgeMorph(live, inc, {});
        report({ text: live.children[0].textContent });
    """, tmp_path)
    assert got["text"] == "Next: new action"


def test_a_branch_becoming_a_leaf_keeps_the_incoming_text(tmp_path):
    """A live node with an element child becomes text-only server-side. The
    old code only set text when `live` ALREADY had zero children -- checked
    before the element was removed -- so this case always failed that check
    and the node ended up empty instead of holding the new text."""
    got = _run(PRELUDE + """
        const live = make("div");
        const p = document.createElement("p");
        p.append(make("time", {}, "x"));
        live.append(p);
        const inc = make("div");
        inc.append(make("p", {}, "just text now"));
        window.bridgeMorph(live, inc, {});
        report({
          text: live.children[0].textContent,
          childElements: live.children[0].children.length,
        });
    """, tmp_path)
    assert got["text"] == "just text now"
    assert got["childElements"] == 0


def test_a_leaf_becoming_a_branch_keeps_the_incoming_element(tmp_path):
    """The reverse transition, for symmetry: a live text-only node gains an
    element child server-side."""
    got = _run(PRELUDE + """
        const live = make("div"); live.append(make("p", {}, "just text"));
        const inc = make("div");
        const p = document.createElement("p");
        p.append(make("time", { id: "t" }, "now"));
        inc.append(p);
        window.bridgeMorph(live, inc, {});
        report({
          text: live.children[0].textContent,
          childId: live.children[0].children[0] &&
            live.children[0].children[0].getAttribute("id"),
        });
    """, tmp_path)
    assert got["text"] == "now"
    assert got["childId"] == "t"


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
