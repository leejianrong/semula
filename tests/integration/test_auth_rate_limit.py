"""The sign-in routes are open to anyone, so each address gets a limit (SEM-115)."""

from pathlib import Path

import httpx
import pytest

from semula.app import create_app
from semula.config import Settings
from semula.oidc import Provider
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration

PROVIDER = Provider("google", "oidc", "client", "secret", issuer="http://127.0.0.1:1")
SECRET = "a-secret-of-at-least-sixteen-bytes"  # noqa: S105
PROXY = ("172.18.0.5", 40000)


def build(tmp_path: Path, **settings: object) -> object:
    return create_app(
        Settings(
            data_dir=tmp_path,
            hosted=True,
            secret=SECRET,
            auth_providers={"google": PROVIDER},
            database_url=fresh_database_url(tmp_path),
            **settings,  # type: ignore[arg-type]
        )
    )


def client(app: object, peer: tuple[str, int] = ("203.0.113.9", 5000)) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer),  # type: ignore[arg-type]
        base_url="http://t",
    )


async def test_the_31st_sign_in_start_in_a_minute_from_one_address_waits(tmp_path: Path) -> None:
    async with client(build(tmp_path)) as c:
        codes = [(await c.get("/auth/login")).status_code for _ in range(30)]
        assert codes == [200] * 30
        blocked = await c.get("/auth/login")
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1
        assert (
            "Too many" in blocked.text
            and "frame-ancestors" in blocked.headers["content-security-policy"]
        )


async def test_every_sign_in_route_shares_the_one_limit(tmp_path: Path) -> None:
    async with client(build(tmp_path)) as c:
        for path in ("/auth/login", "/auth/login/google", "/auth/callback/google") * 10:
            await c.get(path)
        assert (await c.get("/auth/invite/anything")).status_code == 429
        assert (await c.post("/auth/logout")).status_code == 429


async def test_another_address_is_not_held_back_and_the_api_is_not_counted(tmp_path: Path) -> None:
    app = build(tmp_path)
    async with client(app) as one, client(app, ("203.0.113.77", 1)) as two:
        for _ in range(31):
            await one.get("/auth/login")
        assert (await one.get("/auth/login")).status_code == 429
        assert (await two.get("/auth/login")).status_code == 200
        assert (await one.get("/health")).status_code == 200  # not under /auth
        assert (await one.get("/v1/models")).status_code == 401  # the token layer's answer


async def test_behind_a_trusted_proxy_each_client_has_its_own_limit(tmp_path: Path) -> None:
    app = build(tmp_path, trusted_proxies=("172.18.0.0/16",))
    async with client(app, PROXY) as c:
        for _ in range(31):
            await c.get("/auth/login", headers={"X-Forwarded-For": "203.0.113.50"})
        assert (
            await c.get("/auth/login", headers={"X-Forwarded-For": "203.0.113.50"})
        ).status_code == 429
        other = await c.get("/auth/login", headers={"X-Forwarded-For": "203.0.113.51"})
        assert other.status_code == 200


async def test_the_limit_is_a_setting_and_none_removes_it(tmp_path: Path) -> None:
    async with client(build(tmp_path, auth_rate=5)) as c:
        codes = [(await c.get("/auth/login")).status_code for _ in range(6)]
        assert codes == [200] * 5 + [429]
    async with client(build(tmp_path / "x", auth_rate=None)) as c:
        assert all([(await c.get("/auth/login")).status_code == 200 for _ in range(60)])


async def test_a_local_server_has_no_sign_in_routes_to_limit(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, database_url=fresh_database_url(tmp_path)))
    async with client(app) as c:
        assert all([(await c.get("/health")).status_code == 200 for _ in range(60)])
