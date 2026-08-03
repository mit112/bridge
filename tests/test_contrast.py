"""WCAG 2.2 AA contrast over the real stylesheet, in both themes.

Phase 1 verified contrast by hand, which does not survive a token being edited.
This parses `app.css` and computes the ratios, so changing a colour to something
unreadable fails the suite.

The redesign's three-layer token system (primitive -> semantic -> component)
means the semantic names (`--bg`, `--card`, ...) are themselves `var(--p-...)`
aliases, invisible to this module's hex-literal parser. So this suite reads the
PRIMITIVE tokens (`--p-*`) directly -- they are where the real hex lives.
"""

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "app.css"

# (foreground token, background token, minimum ratio, what it is)
PAIRS = [
    ("--p-text", "--p-canvas", 4.5, "body text on canvas"),
    ("--p-text", "--p-surface", 4.5, "body text on a surface"),
    ("--p-text-2", "--p-canvas", 4.5, "secondary text on canvas"),
    ("--p-text-2", "--p-surface", 4.5, "secondary text on a surface"),
    ("--p-work", "--p-surface", 4.5, "work-accent text/link on a surface"),
    ("--p-work", "--p-canvas", 4.5, "work-accent text/link on canvas"),
    ("--p-risk", "--p-surface", 4.5, "risk text on a surface"),
    ("--p-risk", "--p-risk-soft", 4.5, "risk text on risk-soft pill"),
    ("--p-work", "--p-work-soft", 4.5, "work text on work-soft pill"),
    # Non-text UI boundaries need 3:1 (WCAG 1.4.11).
    ("--p-work", "--p-surface", 3.0, "focus ring against a surface"),
    ("--p-field-line", "--p-surface", 3.0, "form control border on a surface"),
    ("--p-field-line", "--p-canvas", 3.0, "form control border on canvas"),
    # The sidebar rail (--p-nav) is dark in BOTH themes, so these pairs are
    # expected to read identically under "light" and "dark" -- that sameness
    # is the point, not a coincidence to fix.
    ("--p-nav-text", "--p-nav", 4.5, "sidebar primary text"),
    ("--p-nav-text-2", "--p-nav", 4.5, "sidebar secondary text"),
    ("--p-nav-accent", "--p-nav", 3.0, "sidebar accent / active indicator"),
    ("--p-nav-focus", "--p-nav", 3.0, "sidebar focus ring"),
]


def _channel(value: float) -> float:
    value /= 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def ratio(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def themes() -> dict[str, dict[str, str]]:
    """Tokens for the light theme and for the dark-mode override block."""
    css = CSS.read_text()
    dark_start = css.index("prefers-color-scheme: dark")
    light_src, dark_src = css[:dark_start], css[dark_start:]

    def tokens(text: str) -> dict[str, str]:
        # Token names may carry a digit (`--p-text-2`), so the name class
        # includes 0-9 alongside the lowercase letters and hyphens; the
        # colour side stays a strict six-digit hex literal.
        return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", text))

    light = tokens(light_src)
    dark = dict(light)
    dark.update(tokens(dark_src))  # dark overrides only what it redefines
    return {"light": light, "dark": dark}


def test_the_stylesheet_defines_both_themes():
    t = themes()
    assert t["light"]["--p-text-2"] != t["dark"]["--p-text-2"], (
        "dark mode must define its own secondary-text colour, not inherit the "
        "light one"
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg,bg,minimum,label", PAIRS)
def test_contrast_meets_wcag_aa(theme, fg, bg, minimum, label):
    tokens = themes()[theme]
    assert fg in tokens, f"{fg} is not defined"
    assert bg in tokens, f"{bg} is not defined"
    got = ratio(tokens[fg], tokens[bg])
    assert got >= minimum, (
        f"{theme}: {label} ({tokens[fg]} on {tokens[bg]}) is {got:.2f}:1, "
        f"below the required {minimum}:1"
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_no_pure_black_or_white_surfaces(theme):
    """anti-pure-black: pure #000/#fff on the opposite extreme is fatiguing."""
    tokens = themes()[theme]
    if theme == "dark":
        assert tokens["--p-canvas"].lower() != "#000000"
        assert tokens["--p-text"].lower() != "#ffffff"
    else:
        assert tokens["--p-text"].lower() != "#000000"
