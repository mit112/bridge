"""The cross-document view transition may never cross-fade two documents.

Three separate attempts fixed the navigation flash by tuning the opacity curves
on `::view-transition-old/new(root)`, and all three failed, because a cross-fade
between two structurally unrelated documents is a DOUBLE EXPOSURE -- not a
mistuned one. Captured frame-by-frame from a real navigation, the Overview and
the Projects ledger were both fully legible, superimposed, for ~60ms.

Both of the curve-tuning failure modes are properties of the cross-fade itself,
which is why neither could be tuned away: let the composite opacity dip below 1
and the page washes out to its own background (a white flash on the cream
theme); hold it at exactly 1 with `mix-blend-mode: plus-lighter` and BOTH
documents render at full strength at once, which is the smear at its worst.

So this module gates the shape of the fix rather than any number in it: every
group whose two sides hold different content swaps instantly, and the one group
that legitimately cross-fades (a single element morphing between two positions)
keeps doing so. Numbers are deliberately not asserted -- durations are Mit's to
tune, and a test that pinned them would fail on taste rather than on a defect.
"""

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "app.css"

# Groups whose old and new snapshots hold DIFFERENT content, so any opacity
# cross-fade between them shows both at once: `root` is the whole page body,
# `app-masthead` carries the <h1> page title, and `app-rail-status` is the
# Jinja block that Overview and project.html override.
DOUBLE_EXPOSURE_RISKS = ["root", "app-masthead", "app-rail-status"]


def stylesheet() -> str:
    """`app.css` with comments removed.

    Not cosmetic: this sheet documents its own reasoning at length, so the
    prose around these rules names the very selectors and properties being
    asserted. Matching against the raw text lets a comment satisfy a test --
    which it did, and it cost a debugging round.
    """
    return re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.DOTALL)


def declarations_for(pseudo: str) -> str:
    """The declaration block of the rule whose selector list includes `pseudo`.

    Selectors are grouped in the sheet (one rule lists all three outgoing
    snapshots), so this matches on membership in the list rather than on an
    exact selector string -- regrouping them differently must not break this.
    """
    css = stylesheet()
    escaped = re.escape(pseudo)
    match = re.search(r"([^{}]*" + escaped + r"[^{}]*)\{([^}]*)\}", css)
    assert match is not None, f"no rule in app.css targets {pseudo}"
    return match.group(2)


@pytest.mark.parametrize("group", DOUBLE_EXPOSURE_RISKS)
def test_the_outgoing_snapshot_is_retired_instantly(group):
    """`animation: none` alone is not enough -- the opacity is what retires it.

    The view-transition pseudo tree survives until the LONGEST animation in the
    whole transition finishes (the nav pill's morph). An un-animated outgoing
    snapshot therefore sits at its default opacity of 1, covering the new page,
    for that entire time. `opacity: 0` is the declaration that actually removes
    it, which is why it is asserted separately rather than assumed to follow.
    """
    block = declarations_for(f"::view-transition-old({group})")
    assert "animation: none" in block, (
        f"::view-transition-old({group}) must not animate -- an authored or UA "
        f"opacity curve here cross-fades two different documents"
    )
    assert re.search(r"opacity:\s*0\b", block), (
        f"::view-transition-old({group}) needs opacity: 0. Without it the "
        f"outgoing snapshot stays fully opaque over the new page until the "
        f"longest animation in the transition ends."
    )


@pytest.mark.parametrize("group", DOUBLE_EXPOSURE_RISKS)
def test_no_group_at_double_exposure_risk_blends(group):
    """`plus-lighter` is only ever meaningful for a cross-fade.

    It exists to make two overlapping snapshots' opacities ADD to full
    strength. On a group that swaps instantly it is inert; on a group that
    cross-fades it is what makes both documents maximally legible at once. So
    finding it on any of these groups means a cross-fade came back.
    """
    for pseudo in (f"::view-transition-old({group})", f"::view-transition-new({group})"):
        block = declarations_for(pseudo)
        assert "plus-lighter" not in block, (
            f"{pseudo} blends with plus-lighter, which only has an effect if it "
            f"is being cross-faded -- and holding the composite at 1.00 is what "
            f"makes the double exposure worst, not what fixes it"
        )


def test_the_moving_nav_pill_keeps_its_cross_fade():
    """The one legitimate cross-fade here, and it must survive the fix.

    `app-nav-active` is ONE element captured at two different positions, so its
    transition describes something moving rather than two unrelated things
    overlaid -- it is the whole reason the swap still reads as continuous. A
    blanket "make everything instant" would take the continuity away with the
    flash, so the absence of a name is the assertion.
    """
    css = stylesheet()
    instant_rules = re.findall(r"([^{}]*::view-transition-[a-z-]+\([^)]*\)[^{}]*)\{([^}]*)\}", css)
    for selectors, block in instant_rules:
        if "animation: none" not in block:
            continue
        assert "app-nav-active" not in selectors, (
            "app-nav-active must keep animating: it is a single element morphing "
            "between two rail positions, which is what carries continuity across "
            "the document swap"
        )
