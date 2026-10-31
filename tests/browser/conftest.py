"""A real browser (Chromium through Playwright) for what only a browser can show: whether text an
attacker stored ever runs. Not in the fast layer: own CI job, ``make test-browser``."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

# Every sink a payload might reach is probed by setting a flag the page never sets itself.
WATCH = """
window.__csp = [];
window.addEventListener('securitypolicyviolation', e =>
  window.__csp.push(e.violatedDirective + ' ' + (e.blockedURI || '') + ' ' +
                    (e.sample || '').slice(0, 60)));
"""


BROWSERS = [b for b in os.environ.get("SEMULA_BROWSERS", "chromium").split(",") if b]


@pytest.fixture(scope="session", params=BROWSERS)
def engine(request: pytest.FixtureRequest) -> Iterator[Any]:
    """``SEMULA_BROWSERS=chromium,firefox,webkit`` runs every test in each engine."""
    with playwright_sync.sync_playwright() as p:
        kind = getattr(p, request.param)
        args = []
        if request.param == "chromium":
            # Chromium blocks one loopback origin from framing another (its local-network
            # rules), which would hide what the framing test is there to see.
            args = [
                "--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests"
            ]
        browser = kind.launch(args=args)
        yield browser
        browser.close()


@pytest.fixture
def new_page(engine: Any) -> Iterator[Any]:
    """``new_page(cookies, url)`` -> a page with the cookies set, recording dialogs and CSP
    violations. Closed after the test."""
    contexts: list[Any] = []

    def make(url: str, cookies: dict[str, str] | None = None) -> Any:
        context = engine.new_context(viewport={"width": 1440, "height": 900})
        contexts.append(context)
        if cookies:
            context.add_cookies([{"name": k, "value": v, "url": url} for k, v in cookies.items()])
        context.add_init_script(WATCH)
        page = context.new_page()
        page.dialogs = []  # type: ignore[attr-defined]
        page.on("dialog", lambda d: (page.dialogs.append(d.message), d.dismiss()))
        page.console_errors = []  # type: ignore[attr-defined]
        page.on("pageerror", lambda e: page.console_errors.append(str(e)))
        return page

    yield make
    for context in contexts:
        context.close()
