# ruff: noqa: F811
"""The pages semula serves itself (sign-in, terms) on a phone (SEM-131): nothing scrolls sideways
and every control is big enough to hit."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.integration.test_security_headers import hosted  # noqa: F401

pytestmark = pytest.mark.browser

PHONE = (390, 844)


@pytest.mark.parametrize("path", ["/auth/login", "/terms", "/no/such/page"])
def test_public_pages_fit_a_phone_and_have_big_controls(
    hosted: Any,
    new_page: Callable[..., Any],
    path: str,
) -> None:
    url, _ = hosted
    page = new_page(url, viewport=PHONE)
    page.goto(url + path)
    assert page.evaluate("document.documentElement.scrollWidth") <= PHONE[0], path
    sizes = page.evaluate(
        "[...document.querySelectorAll('a.btn, button, input, footer a, .brand')]"
        ".map(e => [e.textContent.trim(), e.getBoundingClientRect().height])"
    )
    assert sizes, path
    small = [s for s in sizes if s[1] < 44]
    assert not small, (path, small)
    assert page.evaluate("window.__csp.length") == 0, "the page's policy blocked something"
