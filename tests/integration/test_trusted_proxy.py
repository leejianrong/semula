"""Behind a reverse proxy the socket peer is the proxy; the real client is in X-Forwarded-For,
and only a configured proxy may say so (SEM-111). Driven through the real hosted app."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from semula.app import create_app
from semula.config import Settings
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration

PROXY = ("172.18.0.5", 40000)
BAD = {"Authorization": "Bearer wrong"}

Via = Callable[[tuple[str, int], str | None], httpx.AsyncClient]


class World:
    def __init__(self, service: Service, via: Via) -> None:
        self.service = service
        self.via = via


async def attempts(client: httpx.AsyncClient, n: int, forwarded: str | None) -> httpx.Response:
    headers = dict(BAD)
    if forwarded:
        headers["X-Forwarded-For"] = forwarded
    resp = None
    for _ in range(n):
        resp = await client.get("/v1/models", headers=headers)
    assert resp is not None
    return resp


def build(tmp_path: Path, trusted: tuple[str, ...]) -> tuple[Service, Via]:
    settings = Settings(
        data_dir=tmp_path,
        hosted=True,
        mode="replay",
        database_url=fresh_database_url(tmp_path),
        trusted_proxies=trusted,
    )
    app = create_app(settings)
    service: Service = app.state.service
    service.create_workspace("a")

    def via(peer: tuple[str, int], _unused: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=peer), base_url="http://t"
        )

    return service, via


@pytest.fixture
async def behind(tmp_path: Path) -> AsyncIterator[World]:
    service, via = build(tmp_path, ("172.18.0.0/16",))
    yield World(service, via)


@pytest.fixture
async def unconfigured(tmp_path: Path) -> AsyncIterator[World]:
    service, via = build(tmp_path, ())
    yield World(service, via)


async def test_clients_behind_a_trusted_proxy_are_limited_one_by_one(behind: World) -> None:
    async with behind.via(PROXY, None) as c:
        blocked = await attempts(c, 21, "203.0.113.1")
        assert blocked.status_code == 429
        other = await c.get("/v1/models", headers={**BAD, "X-Forwarded-For": "203.0.113.2"})
        assert other.status_code == 401  # a different person, not locked out by the first


async def test_a_spoofed_leftmost_address_does_not_escape_the_limit(behind: World) -> None:
    async with behind.via(PROXY, None) as c:
        # a client behind the proxy adds its own made-up hop; the proxy appends the real one
        for i in range(21):
            resp = await c.get(
                "/v1/models", headers={**BAD, "X-Forwarded-For": f"10.9.9.{i}, 203.0.113.50"}
            )
        assert resp.status_code == 429  # counted against 203.0.113.50, not the made-up hops


async def test_a_chain_of_trusted_proxies_is_walked_from_the_right(behind: World) -> None:
    async with behind.via(PROXY, None) as c:
        await attempts(c, 21, "203.0.113.60, 172.18.0.9")
        assert (
            await c.get("/v1/models", headers={**BAD, "X-Forwarded-For": "203.0.113.60"})
        ).status_code == 429


async def test_an_untrusted_peer_cannot_choose_its_address(behind: World) -> None:
    async with behind.via(("198.51.100.9", 1234), None) as c:
        for i in range(21):
            resp = await c.get("/v1/models", headers={**BAD, "X-Forwarded-For": f"203.0.113.{i}"})
        assert resp.status_code == 429  # one peer, however many addresses it claims


async def test_without_a_trusted_proxy_the_header_is_ignored(unconfigured: World) -> None:
    async with unconfigured.via(PROXY, None) as c:
        for i in range(21):
            resp = await c.get("/v1/models", headers={**BAD, "X-Forwarded-For": f"203.0.113.{i}"})
        assert resp.status_code == 429


async def test_a_token_records_the_forwarded_network_not_the_proxy(behind: World) -> None:
    svc = behind.service.for_scope(Scope("a"))
    secret = svc.create_token("ci", "replay")["token"]
    async with behind.via(PROXY, None) as c:
        ok = await c.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {secret}", "X-Forwarded-For": "203.0.113.77"},
        )
        assert ok.status_code == 200
    seen = [t["last_used_from"] for t in svc.list_tokens() if t["name"] == "ci"]
    assert seen == ["203.0.113.0/24"]
