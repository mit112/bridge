"""WCAG 2.2 AA contrast over the real stylesheet, in both themes.

Phase 1 verified contrast by hand, which does not survive a token being edited.
This parses `app.css` and computes the ratios, so changing a colour to something
unreadable fails the suite.
"""

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static" / "app.css"

# (foreground token, background token, minimum ratio, what it is)
PAIRS = [
    ("--fg", "--bg", 4.5, "body text"),
    ("--fg", "--card", 4.5, "text on a card"),
    ("--muted", "--card", 4.5, "muted metadata on a card"),
    ("--risk-fg", "--risk-bg", 4.5, "stale warning text"),
    ("--queued-fg", "--queued-bg", 4.5, "queued-handoff heading and status"),
    ("--fg", "--card", 4.5, "copy button label"),
    # Non-text UI: the focus ring and the block's border need 3:1 (WCAG 1.4.11).
    ("--accent", "--card", 3.0, "focus ring against a card"),
    ("--queued-line", "--queued-bg", 3.0, "queued block border"),
    # Phase 3: a form control's visible boundary. `--line` measures 1.34:1 light
    # and 1.28:1 dark against --card, so the selects and the prompt field get
    # their own token and it is computed here rather than eyeballed.
    ("--field-line", "--card", 3.0, "form control border"),
    # The prompt field's border is adjacent to two surfaces: its own --card fill
    # and the queued block it sits in, so both sides are held to 3:1.
    ("--field-line", "--queued-bg", 3.0, "prompt field border in a queued block"),
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
        return dict(re.findall(r"(--[a-z-]+):\s*(#[0-9a-fA-F]{6})", text))

    light = tokens(light_src)
    dark = dict(light)
    dark.update(tokens(dark_src))  # dark overrides only what it redefines
    return {"light": light, "dark": dark}


def test_the_stylesheet_defines_both_themes():
    t = themes()
    assert t["light"]["--queued-fg"] != t["dark"]["--queued-fg"], (
        "dark mode must define its own queued colours, not inherit the light ones"
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
        assert tokens["--bg"].lower() != "#000000"
        assert tokens["--fg"].lower() != "#ffffff"
    else:
        assert tokens["--fg"].lower() != "#000000"
