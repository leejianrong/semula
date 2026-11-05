import json
from pathlib import Path

import pytest

from semula.dashboard.theme import DARK, LIGHT, SOURCES

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[2]
TOKENS = ROOT / "docs" / "design" / "tokens.json"


def test_every_theme_token_is_defined_for_both_schemes() -> None:
    assert set(LIGHT) == set(DARK) == set(SOURCES)


@pytest.mark.skipif(not TOKENS.exists(), reason="the design tokens are not in this checkout")
def test_the_theme_equals_the_design_tokens() -> None:
    design = json.loads(TOKENS.read_text())
    for scheme, ours, column in (("light", LIGHT, 0), ("dark", DARK, 1)):
        for token, value in ours.items():
            role = SOURCES[token][column]
            assert value.lower() == design[scheme][role].lower(), (scheme, token, role)


def test_the_theme_is_accepted_by_indah() -> None:
    from semula.dashboard.theme import build_theme

    css = build_theme().css()
    assert "--primary: #256A59" in css and "--err: #C8271F" in css


@pytest.mark.skipif(not (ROOT / "docs" / "design" / "logo").exists(), reason="no design files")
def test_the_packaged_logo_keeps_the_record_dot() -> None:
    packaged = (ROOT / "src/semula/dashboard/semula-mark.svg").read_text()
    original = (ROOT / "docs/design/logo/semula-mark.svg").read_text()
    assert "#C8271F" in packaged and "#C8271F" in original


FONTS = ROOT / "src/semula/dashboard/fonts"


def test_the_theme_sets_the_design_type_faces_and_serves_hanken_itself() -> None:
    """Display Bricolage Grotesque, body Hanken Grotesk, data IBM Plex Mono (docs/design). The
    first and last ship inside indah's page; Hanken is our file, served from our own origin."""
    from semula.dashboard.theme import build_theme

    theme = build_theme()
    css = theme.css()
    assert '--font-display: "Bricolage Grotesque"' in css
    assert '--font-body: "Hanken Grotesk"' in css
    assert '--font-mono: "IBM Plex Mono"' in css
    assert "font-display: swap" in css and "url(" in css
    assert "http" not in css.split("@font-face", 1)[1].split("}", 1)[0], "no remote font"
    assert len(theme.font_files) == 1


def test_the_font_file_is_a_small_woff2_with_its_licence_beside_it() -> None:
    (font,) = FONTS.glob("*.woff2")
    data = font.read_bytes()
    assert data[:4] == b"wOF2"
    assert len(data) < 60_000, "a latin subset, not the whole family"
    assert "SIL Open Font License" in (FONTS / "OFL.txt").read_text()
    readme = (FONTS / "README.md").read_text()
    assert "OFL-1.1" in readme and "fontsource" in readme
