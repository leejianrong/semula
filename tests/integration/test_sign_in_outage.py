"""A sign-in provider that cannot be reached is an ordinary bad day (SEM-114: a ZAP scan found
``GET /auth/login/google`` answering a bare 500). The person gets a page that says what happened
and how to try again, with the same security headers as the other sign-in pages."""

from pathlib import Path

import httpx
import pytest

from semula import oidc
from semula.app import create_app
from semula.config import Settings
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration

SECRET = "a-secret-of-at-least-sixteen-bytes"  # noqa: S105
# nothing listens here: the connection is refused at once, like a provider that is down
DOWN = oidc.Provider("google", "oidc", "client", "secret", issuer="http://127.0.0.1:1")
PUBLIC = "http://127.0.0.1:8080"


def client(tmp_path: Path) -> httpx.AsyncClient:
    settings = Settings(
        data_dir=tmp_path,
        hosted=True,
        secret=SECRET,
        public_url=PUBLIC,
        auth_providers={"google": DOWN},
        database_url=fresh_database_url(tmp_path),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url=PUBLIC
    )


def says_so(resp: httpx.Response) -> None:
    assert resp.status_code == 502, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert "could not be reached" in resp.text and "/auth/login" in resp.text
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert "Traceback" not in resp.text and "127.0.0.1:1" not in resp.text


async def test_starting_a_sign_in_while_the_provider_is_down(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        says_so(await c.get("/auth/login/google"))


async def test_finishing_a_sign_in_while_the_provider_is_down(tmp_path: Path) -> None:
    flow, _ = oidc.new_flow(DOWN, f"{PUBLIC}/auth/callback/google", "/ui/")
    async with client(tmp_path) as c:
        c.cookies.set("semula_flow", oidc.seal(SECRET.encode(), flow))
        says_so(await c.get("/auth/callback/google", params={"code": "c", "state": flow["state"]}))
