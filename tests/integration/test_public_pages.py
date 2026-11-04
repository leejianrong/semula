# ruff: noqa: F811
"""Every page semula serves itself shares one styled page (SEM-131)."""

import re

import httpx
import pytest

from semula import api_auth, public_page
from tests.integration.test_security_headers import csp, hosted  # noqa: F401

pytestmark = pytest.mark.integration

PATHS = ["/auth/login", "/terms", "/auth/signup", "/auth/device?code=nope", "/auth/invite/nope"]


def fetch(hosted, path: str) -> httpx.Response:  # type: ignore[no-untyped-def]
    url, cookies = hosted
    return httpx.get(url + path, cookies=cookies, timeout=20, headers={"accept": "text/html"})


@pytest.mark.parametrize("path", [*PATHS, "/no/such/page"])
def test_every_public_page_uses_the_shared_shell(hosted, path: str) -> None:  # type: ignore[no-untyped-def]
    resp = fetch(hosted, path)
    assert "text/html" in resp.headers["content-type"], path
    text = resp.text
    assert public_page.STYLE in text and public_page.MARK in text, path
    assert public_page.DESCRIPTION in text and "<h1>" in text, path
    assert "prefers-color-scheme:dark" in text, path
    assert "name=viewport" in text, path
    # nothing is fetched: no URL in an attribute, no import, no script
    assert not re.search(r"(?:src|href)=['\"]?(?:https?:)?//", text), path
    assert "@import" not in text and "<script" not in text and "http://" not in text, path
    assert csp(resp)["default-src"] == "'none'"
    assert f"'sha256-{public_page.STYLE_HASH}'" in csp(resp)["style-src"]


def test_a_missing_page_is_html_for_a_browser_and_json_for_a_client(hosted) -> None:  # type: ignore[no-untyped-def]
    url, _ = hosted
    assert fetch(hosted, "/no/such/page").status_code == 404
    api = httpx.get(url + "/no/such/page", timeout=20)
    assert api.status_code == 404 and api.json()["error"]["code"] == "not_found"
    assert httpx.get(url + "/v1/nope", headers={"accept": "text/html"}, timeout=20).json()


def test_the_title_is_escaped() -> None:
    assert "<script>" not in public_page.render("<script>x</script>", "")


def test_the_device_page_escapes_a_hostile_name() -> None:
    hostile = "<img src=x onerror=alert(1)>"
    preview = {"scope": "replay", "project": hostile, "user_code": hostile, "name": hostile}
    page = api_auth._device_page(preview, "admin", hostile, hostile).body.decode()
    assert hostile not in page and "&lt;img" in page
    assert "<button class=alt name=decision value=deny>" in page
