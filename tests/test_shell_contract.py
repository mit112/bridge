"""The architectural rules a content swap depends on, asserted against the source.

These enforce the RULE rather than simulating its consequences: a file that
captures a DOM node at module scope is broken after the first swap no matter what
any behavioural test happens to exercise. Cheap, fast, and impossible to satisfy
with a vacuous stub.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static"
TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "bridge" / "templates"

PAGE_SCRIPTS = ["shell.js", "copy.js", "launch.js", "schedule.js",
                "live.js", "projects.js", "settings.js", "router.js",
                "liverefresh.js"]

def source(name: str) -> str:
    """Source with comments stripped.

    Stripping is not cosmetic: these files document themselves using the exact
    identifiers under test, and a previous test in this repo passed because its
    regex matched the PROSE of a comment rather than the code.
    """
    text = (STATIC / name).read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


@pytest.mark.parametrize("name", PAGE_SCRIPTS)
def test_no_domcontentloaded_outside_the_router(name):
    """DOMContentLoaded fires once per DOCUMENT, and there is now one document.

    schedule.js bound its timezone painting to it, so every scheduled time would
    render in raw UTC on any page reached by a swap.
    """
    if name in ("shell.js", "router.js"):
        if not (STATIC / name).exists():
            pytest.skip("router.js lands in task 9")
        pytest.skip(f"{name} is the router/registry itself, not a page script")
    assert "DOMContentLoaded" not in source(name), (
        f"{name} binds DOMContentLoaded, which fires once per document -- it will "
        f"never fire again after the first swap. Register a bridgePage.onEnter hook."
    )


@pytest.mark.parametrize("name", PAGE_SCRIPTS)
def test_no_module_scope_dom_capture(name):
    """A node captured at load is detached forever after the first swap.

    Catches two spellings of the same bug: a module-scope `const`/`let`/`var`
    initialized directly from `document.querySelector(All)?`/`getElementById`,
    OR initialized from a call to a same-file helper whose ENTIRE body is
    nothing but `return document.<lookup>(...)`. live.js's real violation is
    the second spelling: `function query(selector) { return
    document.querySelector(selector); }` plus, at module scope, `const
    initialStrip = query("[data-freshness-strip]");` -- a direct-only check
    cannot see through that one level of indirection, but the node it
    captures is exactly as detached after the first swap either way.

    Deliberately narrow so it does not flag every module-scope call: a helper
    only counts if its body is PURELY a DOM-lookup forward, so live.js's own
    `connect()` (which opens an `EventSource` and does a great deal more) is
    correctly left alone, and a future router.js's `new Set(["/", "/projects",
    ...])` is a literal array of URL strings passed to the built-in `Set`, not
    a call to any same-file DOM-lookup wrapper -- neither is this rule's
    concern, and neither is flagged.
    """
    if not (STATIC / name).exists():
        pytest.skip("router.js lands in task 9")
    text = source(name)

    dom_lookup = r"document\.(?:querySelector|querySelectorAll|getElementById)\b"
    wrapper_names = re.findall(
        rf"^function\s+(\w+)\s*\([^)]*\)\s*\{{\s*return\s+{dom_lookup}\s*\([^;]*;\s*\}}\s*$",
        text, re.M,
    )
    alternatives = [dom_lookup] + [rf"\b{re.escape(n)}\s*\(" for n in wrapper_names]
    pattern = re.compile(
        r"^(?:const|let|var)\s+(\w+)\s*=\s*[^;]*?(?:" + "|".join(alternatives) + r")",
        re.M,
    )
    hits = pattern.findall(text)
    assert not hits, (
        f"{name} captures a DOM node at module scope ({hits}). That node is "
        f"detached after the first content swap. Look it up inside the handler "
        f"or inside a bridgePage.onEnter hook instead."
    )


@pytest.mark.parametrize(
    "name", ["settings.js", "schedule.js", "projects.js", "launch.js", "live.js",
             "liverefresh.js"]
)
def test_per_page_behaviour_is_registered_on_the_registry(name):
    assert "bridgePage.onEnter" in source(name), (
        f"{name} has per-page-view setup that must re-run after a swap, but "
        f"registers no bridgePage.onEnter hook."
    )


def test_launch_flushes_pending_edits_before_the_swap():
    """Removing a focused node does not fire focusout in ANY browser.

    launch.js saves an edited handoff prompt on focusout. A full document
    navigation fires it; detaching the node does not. Without an onLeave flush
    the user's edit is silently discarded -- and the prompt is the one thing
    Bridge cannot rebuild from transcripts.
    """
    assert "bridgePage.onLeave" in source("launch.js"), (
        "launch.js registers no onLeave hook, so a prompt edited and then "
        "navigated away from is lost with no PATCH and no warning"
    )


def test_no_template_overrides_the_scripts_block():
    """The script-injection path is gone; nothing may reintroduce it.

    A swap never evaluates `{% block scripts %}`, so a page whose JS arrives that
    way is simply inert. settings.js was the only user and now loads from base.
    """
    offenders = [
        p.name for p in TEMPLATES.glob("*.html")
        if p.name != "base.html" and "block scripts" in p.read_text()
    ]
    assert offenders == [], (
        f"{offenders} override the scripts block, which a content swap never "
        f"evaluates -- that page's JS would never run. Load it from base.html."
    )


def test_router_intercepts_the_sidebar_destinations_and_the_project_workspace():
    """The workspace has a fragment mode now, so the router swaps it -- and its
    tabs and the history tables' sort/filter/pager, which all share the
    /project/{id} path -- instead of tearing down the shell on every click. The
    five sidebar destinations stay swappable, and the workspace match is scoped
    to a numeric id so it can never widen to some other /project/... subpath.
    """
    text = source("router.js")
    assert "SWAPPABLE" in text
    for path in ('"/"', '"/projects"', '"/schedule"', '"/diagnostics"', '"/settings"'):
        assert path in text
    # The workspace path is matched by a numeric-id regex, not added to the Set
    # (comments are stripped by `source`, so this sees only real code).
    assert "WORKSPACE_PATH" in text
    assert r"\/project\/" in text


def test_router_lands_a_swap_at_the_top_without_a_focus_scroll():
    """A swap must land at the top of the page like a real navigation.

    At >=1024px `.shell__body` -- not the window -- is the scroll container (the
    shell is a fixed 100vh cage), so `window.scrollTo(0,0)` alone is a no-op
    there; and focusing a `#main` taller than the viewport scrolls that
    container to pin #main's top, pushing the page header out of view. The
    router must therefore focus with `preventScroll` and reset the
    `.shell__body` scroll position itself. `source` strips comments, so these
    tokens are asserted against real code. Reproduced in situ: without the fix a
    filter/tab swap while scrolled down lands `.shell__body` at ~139px (the
    page-head height) instead of 0.
    """
    text = source("router.js")
    assert "preventScroll" in text, (
        "focus() without preventScroll scrolls the tall #main's top to the top "
        "of the .shell__body container, hiding the page header after a swap"
    )
    assert ".scrollTop = 0" in text, (
        "the router must reset the .shell__body scroll container to the top; "
        "window.scrollTo(0,0) is a no-op against it at >=1024px"
    )


def test_router_falls_back_to_a_normal_navigation():
    text = source("router.js")
    assert "location.assign" in text, (
        "the router must fall back to a real navigation on any failure, or a "
        "server error leaves the user on a page whose link did nothing"
    )


def test_router_awaits_the_leave_flush_before_swapping():
    """Carried finding from Task 6, resolved here: a leave hook's async flush
    (launch.js's prompt save) must settle before the swap discards the node it
    warns through, or a failed save's warning is lost with nothing left to
    announce through. bridgePage.leave() (Task 1) now returns a promise that
    settles once every hook's own async work has settled; this asserts the
    router actually awaits it rather than firing the swap alongside it.

    tests/test_swap_lifecycle.py::test_a_failed_leave_flush_still_surfaces_its_warning_before_the_swap
    proves the mechanism (bridgePage.leave()'s new contract) behaviourally; this
    is the static check that router.js actually uses it.
    """
    assert "await window.bridgePage.leave()" in source("router.js"), (
        "navigate() must await bridgePage.leave() before fetching the fragment, "
        "or a leave hook's pending flush can lose its warning to the swap"
    )


def test_settings_js_is_loaded_from_base():
    """An actual `<script src>` tag, not just the filename anywhere in the file.

    base.html's own FOUC-guard comment says (in prose) that it duplicates
    "settings.js's own apply logic" -- a substring check on the raw file would
    pass on that sentence alone without settings.js ever being loaded. Requiring
    the literal tag text is what tells the two apart.
    """
    assert '<script src="/static/settings.js"' in (TEMPLATES / "base.html").read_text()


def test_every_selector_is_one_the_harness_can_parse():
    """Keeps the layer-3 harness honest about the code it claims to model.

    minidom.js supports tag/class/id/attribute parts and descendant combinators.
    If a source file starts using `>`, `+`, `~` or `:pseudo`, the harness would
    silently mis-model it -- so that is a failure here, not a surprise there.
    """
    literal = re.compile(r"""querySelector(?:All)?\(\s*["'`]([^"'`]+)["'`]""")
    unsupported = re.compile(r"[>+~]|::?[a-z-]+\(?")
    bad = []
    for name in PAGE_SCRIPTS:
        if not (STATIC / name).exists():
            continue
        for sel in literal.findall(source(name)):
            if unsupported.search(sel):
                bad.append((name, sel))
    assert bad == [], (
        f"selector forms the mini-DOM cannot parse: {bad}. Either simplify the "
        f"selector or teach tests/js/minidom.js the form -- do not leave the "
        f"harness silently mis-modelling the code under test."
    )


def test_router_exposes_the_fragment_parser_for_reuse():
    """liverefresh.js reuses router.js's parser instead of shipping a second one."""
    text = source("router.js")
    assert "window.bridgeFragment" in text, (
        "router.js must expose its fragment parser as window.bridgeFragment "
        "so the live-refresh controller can reuse it"
    )
