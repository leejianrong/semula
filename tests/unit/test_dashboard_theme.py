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
