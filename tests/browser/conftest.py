"""A real browser (Playwright: Chromium, Firefox, WebKit) for what only a browser can show: whether
text an attacker stored ever runs, and whether the dashboard can be used on a phone and with the
keyboard alone. Not in the fast layer: own CI job, ``make test-browser``."""

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
        try:
            browser = kind.launch(args=args)
        except Exception as exc:
            if os.environ.get("CI"):
                raise  # in CI every engine asked for must start
            # WebKit needs system libraries that a laptop often lacks; say so rather than fail
            pytest.skip(f"{request.param} cannot start here: {str(exc).splitlines()[0]}")
        yield browser
        browser.close()


@pytest.fixture
def new_page(engine: Any) -> Iterator[Any]:
    """``new_page(url, cookies, viewport)`` -> a page with the cookies set, recording dialogs and
    CSP violations. Closed after the test."""
    contexts: list[Any] = []

    def make(
        url: str, cookies: dict[str, str] | None = None, viewport: tuple[int, int] = (1440, 900)
    ) -> Any:
        context = engine.new_context(viewport={"width": viewport[0], "height": viewport[1]})
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
