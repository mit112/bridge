"""Fonts are bundled locally, never fetched from a CDN.

`app.css` must declare `@font-face` rules pointing at woff2 files under
`static/fonts/`, each with a real OFL licence beside it, and never reference
`fonts.googleapis.com`/`fonts.gstatic.com`. A regression that swapped this for
a Google Fonts `<link>` would still render correctly on a machine with
internet access, so nothing short of asserting the CSS text itself catches it.
"""

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "bridge" / "static"
CSS = (STATIC / "app.css").read_text()


def test_fonts_are_self_hosted_not_cdn():
    assert "fonts.googleapis.com" not in CSS and "fonts.gstatic.com" not in CSS
    assert "@font-face" in CSS
    assert 'src: url("/static/fonts/' in CSS


def test_font_files_and_licenses_present():
    fonts = STATIC / "fonts"
    assert (fonts / "OFL-Atkinson.txt").exists()
    assert (fonts / "OFL-IBMPlexMono.txt").exists()
    assert list(fonts.glob("atkinson-hyperlegible-next-*.woff2"))
    assert list(fonts.glob("ibm-plex-mono-*.woff2"))


def test_font_face_uses_swap():
    assert CSS.count("font-display: swap") >= 2


def test_fraunces_display_font_present():
    fonts = STATIC / "fonts"
    assert (fonts / "OFL-Fraunces.txt").exists()
    assert list(fonts.glob("fraunces-*.woff2"))
    # Provenance is recorded, not just the bytes dropped in (matches the
    # convention the existing fonts follow — a source + SHA-256 per file).
    provenance = (fonts / "PROVENANCE.md").read_text()
    assert "Fraunces" in provenance
    assert "fraunces-semibold-600.woff2" in provenance


def test_fraunces_declared_in_css():
    assert 'font-family: "Fraunces"' in CSS
    assert "/static/fonts/fraunces-" in CSS
