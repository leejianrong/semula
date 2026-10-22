"""Waiting for the next parked call (SEM-75): REST, service, MCP and CLI."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

from semula.cli import main
from tests.integration.conftest import chat
from tests.support.server import running_server

pytestmark = pytest.mark.integration


async def test_a_parked_call_wakes_a_waiter_within_a_second(record: httpx.AsyncClient) -> None:
    waiter = asyncio.create_task(record.get("/api/v1/pending", params={"wait": 30}, timeout=40))
    await asyncio.sleep(0.3)
    assert not waiter.done()
    started = time.monotonic()
    call = asyncio.create_task(record.post("/v1/chat/completions", json=chat("wake up")))
    resp = await asyncio.wait_for(waiter, 5)
    assert time.monotonic() - started < 1.0
    items = resp.json()["pending"]
    assert [p["status"] for p in items] == ["pending"]
    await record.post(f"/api/v1/pending/{items[0]['id']}/answer", json={"content": "ok"})
    await call


async def test_what_already_waits_is_returned_at_once(record: httpx.AsyncClient) -> None:
    call = asyncio.create_task(record.post("/v1/chat/completions", json=chat("already")))
    for _ in range(300):
        if (await record.get("/api/v1/pending")).json()["pending"]:
            break
        await asyncio.sleep(0.01)
    started = time.monotonic()
    resp = await record.get("/api/v1/pending", params={"wait": 30})
    assert time.monotonic() - started < 1.0
    assert len(resp.json()["pending"]) == 1
    await record.post(
        f"/api/v1/pending/{resp.json()['pending'][0]['id']}/answer", json={"content": "ok"}
    )
    await call


async def test_the_wait_ends_empty_at_the_timeout(record: httpx.AsyncClient) -> None:
    started = time.monotonic()
    resp = await record.get("/api/v1/pending", params={"wait": 0.5})
    assert resp.json() == {"pending": []}
    assert 0.4 < time.monotonic() - started < 3


async def test_a_waiter_for_another_project_is_not_woken(record: httpx.AsyncClient) -> None:
    await record.post("/api/v1/projects", json={"id": "other"})
    waiter = asyncio.create_task(
        record.get("/api/v1/pending", params={"wait": 1, "project": "other"}, timeout=10)
    )
    await asyncio.sleep(0.2)
    call = asyncio.create_task(record.post("/v1/chat/completions", json=chat("elsewhere")))
    assert (await waiter).json() == {"pending": []}
    pending_id = (await record.get("/api/v1/pending")).json()["pending"][0]["id"]
    await record.post(f"/api/v1/pending/{pending_id}/answer", json={"content": "ok"})
    await call


async def test_a_wait_over_the_limit_is_refused(record: httpx.AsyncClient) -> None:
    resp = await record.get("/api/v1/pending", params={"wait": 61})
    assert resp.status_code == 422 or resp.status_code == 400


async def test_a_waiting_request_holds_no_database_connection(
    record: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = record._transport.app.state.service  # type: ignore[attr-defined]
    active = 0
    for name in ("rows", "one", "begin"):
        original = getattr(service.db, name)

        def spy(*a: Any, _orig: Any = original, **k: Any) -> Any:
            nonlocal active
            active += 1
            try:
                return _orig(*a, **k)
            finally:
                active -= 1

        monkeypatch.setattr(service.db, name, spy)
    waiter = asyncio.create_task(record.get("/api/v1/pending", params={"wait": 1.5}, timeout=10))
    await asyncio.sleep(0.5)
    assert not waiter.done() and active == 0
    await waiter


def test_the_mcp_tool_waits(tmp_path: Path) -> None:
    async def go(url: str) -> None:
        async with Client(url + "/mcp/") as client:
            started = time.monotonic()
            waiter = asyncio.create_task(
                client.call_tool("wait_for_pending", {"timeout_seconds": 30})
            )
            await asyncio.sleep(0.3)
            async with httpx.AsyncClient(base_url=url, timeout=10) as http:
                call = asyncio.create_task(http.post("/v1/chat/completions", json=chat("via mcp")))
                result = await asyncio.wait_for(waiter, 5)
                assert time.monotonic() - started < 2
                items = _data(result)
                assert len(items) == 1
                await http.post(f"/api/v1/pending/{items[0]['id']}/answer", json={"content": "ok"})
                await call
            empty = await client.call_tool("wait_for_pending", {"timeout_seconds": 0.3})
            assert _data(empty) == []

    with running_server(tmp_path, mode="record") as url:
        asyncio.run(go(url))


def _data(result: Any) -> Any:
    return result.data if result.data is not None else json.loads(result.content[0].text)


def test_the_cli_waits(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with running_server(tmp_path, mode="record") as url:
        started = time.monotonic()
        assert main(["--url", url, "pending", "--wait", "0.5", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == []
        assert time.monotonic() - started >= 0.4
        assert main(["--url", url, "pending", "--wait", "0.5"]) == 0
        assert "no pending requests" in capsys.readouterr().out
