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


def without_reduced_motion(css: str) -> str:
    """`css` with the `prefers-reduced-motion: reduce` block removed.

    That block deliberately switches EVERY group off, including the one that is
    supposed to keep animating, so a test asking "what still moves normally?"
    has to read past it. Brace-counted rather than regexed because the block
    contains nested rules.
    """
    marker = "@media (prefers-reduced-motion: reduce)"
    start = css.find(marker)
    if start == -1:
        return css
    depth, i = 0, css.index("{", start)
    for i in range(i, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                break
    return css[:start] + css[i + 1:]


def declarations_for(pseudo: str) -> str:
    """The declaration block of the rule whose selector list includes `pseudo`.

    Selectors are grouped in the sheet (one rule lists all three outgoing
    snapshots), so this matches on membership in the list rather than on an
    exact selector string -- regrouping them differently must not break this.

    The reduced-motion block is skipped: it names every group, so it would
    always be the first match and these callers ask about the UNCONDITIONAL
    behaviour.
    """
    css = without_reduced_motion(stylesheet())
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


def test_the_navigation_opt_in_is_not_nested_in_a_media_query():
    """Wrapping `@view-transition` in `@media` kills the whole transition in Arc.

    Arc PARSES the nested form -- it shows up in the CSSOM with the media query
    evaluating true -- and then does not honour it: `pageswap` reported
    `event.viewTransition` null on every navigation, and un-nesting it with no
    other change made the same probe report non-null. Support for this at-rule
    inside a conditional group rule landed after the feature itself, and Arc
    reports a Chrome version ahead of the engine it ships, so nothing about the
    nested form looks wrong from the outside.

    It cost a full session of fixes aimed at a transition that was not running.
    Reduced motion is handled by neutralising the animations instead, which is
    why this can be asserted unconditionally.
    """
    css = stylesheet()
    at_rule = css.index("@view-transition")
    enclosing = css.rfind("@media", 0, at_rule)
    if enclosing == -1:
        return  # no @media precedes it at all, so it cannot be nested in one
    # Nested iff a brace opened by that @media is still unclosed at the at-rule.
    depth = 0
    for ch in css[enclosing:at_rule]:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    assert depth == 0, (
        "@view-transition is nested inside an @media block. Arc parses that and "
        "silently refuses to run the transition, so every rule below it becomes "
        "dead code in the browser this app is actually used in. Keep the opt-in "
        "at the top level and gate reduced motion on the animations instead."
    )


def reduced_motion_block() -> str:
    """Just the `prefers-reduced-motion: reduce` block, braces and all."""
    css = stylesheet()
    start = css.index("@media (prefers-reduced-motion: reduce)")
    depth = 0
    for end in range(css.index("{", start), len(css)):
        if css[end] == "{":
            depth += 1
        elif css[end] == "}":
            depth -= 1
            if depth == 0:
                return css[start:end + 1]
    raise AssertionError("unterminated reduced-motion block in app.css")


def named_groups() -> set[str]:
    """Every view-transition group in the sheet, derived rather than listed.

    `root` always exists implicitly; the rest come from the actual
    `view-transition-name` declarations, so adding a named group and forgetting
    the reduced-motion list below fails this suite instead of shipping motion to
    someone who asked for none.
    """
    names = set(re.findall(r"view-transition-name:\s*([a-z0-9-]+)", stylesheet()))
    names.discard("none")
    return names | {"root"}


@pytest.mark.parametrize("group", sorted(named_groups()))
def test_reduced_motion_leaves_nothing_animating(group):
    """A stated motion preference must reach EVERY group, not just most of them.

    With the content swap already instant for everyone, the only thing reduced
    motion still has to suppress is the sliding nav pill and the rail's
    (invisible) cross-fade -- but "only a little motion" is not what the
    preference asks for. Asserted per group so a newly named one cannot slip
    through: the pill was exactly the group a blanket rule would have missed.
    """
    block = reduced_motion_block()
    rules = re.findall(r"([^{}]*)\{([^{}]*)\}", block)
    # Rule-level, not substring-level: naming a group in the block proves
    # nothing if the declaration next to it is still a duration. A first pass
    # here only checked membership, and a mutation swapping `animation: none`
    # for `animation-duration: 90ms` survived it.
    silenced = [s for s, decls in rules if "animation: none" in decls]
    for pseudo in ("group", "old", "new"):
        assert any(f"::view-transition-{pseudo}({group})" in s for s in silenced), (
            f"::view-transition-{pseudo}({group}) is not switched off under "
            f"prefers-reduced-motion: reduce -- it must appear in a rule that "
            f"sets `animation: none`, not merely be mentioned in the block"
        )
    # The outgoing snapshot needs retiring as well as un-animating, for the same
    # reason it does unconditionally: un-animated, it sits at opacity 1 on top.
    # So find the rule that actually sets opacity: 0 and check this group is in
    # ITS selector list, rather than just that both strings occur somewhere.
    retiring = [
        selectors for selectors, decls
        in re.findall(r"([^{}]*)\{([^{}]*)\}", block)
        if re.search(r"opacity:\s*0\b", decls)
    ]
    assert retiring, "reduced motion never retires the old snapshots"
    assert any(f"::view-transition-old({group})" in s for s in retiring), (
        f"::view-transition-old({group}) is un-animated under reduced motion but "
        f"never given opacity: 0, so it covers the new page for the whole "
        f"transition"
    )


def test_the_moving_nav_pill_keeps_its_cross_fade():
    """The one legitimate cross-fade here, and it must survive the fix.

    `app-nav-active` is ONE element captured at two different positions, so its
    transition describes something moving rather than two unrelated things
    overlaid -- it is the whole reason the swap still reads as continuous. A
    blanket "make everything instant" would take the continuity away with the
    flash, so the absence of a name is the assertion.
    """
    css = without_reduced_motion(stylesheet())
    instant_rules = re.findall(r"([^{}]*::view-transition-[a-z-]+\([^)]*\)[^{}]*)\{([^}]*)\}", css)
    for selectors, block in instant_rules:
        if "animation: none" not in block:
            continue
        assert "app-nav-active" not in selectors, (
            "app-nav-active must keep animating: it is a single element morphing "
            "between two rail positions, which is what carries continuity across "
            "the document swap"
        )
