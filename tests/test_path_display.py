"""Every surface that shows a project path must clip the PREFIX, not the tail.

`/Users/you/dev/Job apps/instalily` and `/Users/you/dev/Job apps/instabase` are
different projects that share every character up to the last four. Clip on the
right and both render `/Users/you/dev/Job apps/insta…` -- the surviving text is
the part they have in common, so the reader is shown nothing that tells them
apart. The distinguishing part is the leaf, so the leaf is what has to survive.

There is no Jinja filter to test here and deliberately so: truncation is done by
the browser, against the real rendered width, and a filter would have to guess a
character count that is wrong at every width but one. The rule is a CSS
declaration, so this asserts on the CSS -- the same way `test_components.py`
asserts that every status word has a pill rule.
"""

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1]
       / "src" / "bridge" / "static" / "app.css").read_text()


def _block(selector: str) -> str:
    match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{(.*?)\}}", CSS, re.S)
    assert match, f"no `{selector}` rule in app.css"
    return match.group(1)


def test_the_project_path_truncates_from_the_left_on_every_surface():
    """The base rule, which is what the Overview's Recent projects list uses.
    The Projects index used to be the only place that set this."""
    block = _block(".project-row__path")
    assert "text-overflow: ellipsis" in block
    assert "direction: rtl" in block, (
        "the base rule clips the right-hand end, so two projects under the same "
        "parent render as the same string on the Overview"
    )
    assert "text-align: left" in block, "rtl without this right-aligns the run"
