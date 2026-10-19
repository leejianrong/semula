"""Async handlers must not block on the database, and answers may arrive on any thread."""

import asyncio
import time
from pathlib import Path

import pytest

from semula import canonical
from semula.app import create_app
from semula.config import Settings
from tests.integration.conftest import chat, make_client
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration


async def test_a_slow_query_does_not_block_other_requests(tmp_path: Path) -> None:
    async with make_client(tmp_path, mode="replay") as client:
        service = client._transport.app.state.service  # type: ignore[attr-defined]
        real_find = service.find

        def slow_find(*args: object, **kwargs: object) -> object:
            time.sleep(0.6)  # stands in for a slow database round trip
            return real_find(*args, **kwargs)

        service.find = slow_find
        slow = asyncio.create_task(client.post("/v1/chat/completions", json=chat("slow")))
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        health = await client.get("/health")
        elapsed = time.perf_counter() - started
        assert health.status_code == 200
        assert elapsed < 0.3, f"/health waited {elapsed:.2f}s behind a slow query"
        assert (await slow).status_code == 424


async def test_an_answer_from_another_thread_releases_the_held_call(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, database_url=fresh_database_url(tmp_path)))
    service = app.state.service
    req = canonical.from_openai(chat("held"))
    pid = await asyncio.to_thread(service.open_pending, req)
    waiter = asyncio.create_task(service.wait_for_answer(pid, 5.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await asyncio.to_thread(service.answer_pending, pid, {"content": "from a thread"})
    stored = await asyncio.wait_for(waiter, 2.0)
    assert stored is not None and stored["message"]["content"] == "from a thread"


async def test_an_answer_that_lands_before_the_wait_starts_is_not_missed(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, database_url=fresh_database_url(tmp_path)))
    service = app.state.service
    pid = await asyncio.to_thread(service.open_pending, canonical.from_openai(chat("early")))
    await asyncio.to_thread(service.answer_pending, pid, {"content": "already here"})
    started = time.perf_counter()
    stored = await service.wait_for_answer(pid, 5.0)
    assert stored is not None and stored["message"]["content"] == "already here"
    assert time.perf_counter() - started < 1.0  # did not sit out the timeout


async def test_several_held_calls_on_one_request_are_all_released(tmp_path: Path) -> None:
    async with make_client(tmp_path, mode="record", record_timeout=5.0) as client:
        calls = [
            asyncio.create_task(client.post("/v1/chat/completions", json=chat("crowd")))
            for _ in range(4)
        ]
        pending: list[dict[str, object]] = []
        for _ in range(100):
            pending = (await client.get("/api/v1/pending")).json()["pending"]
            if pending:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        await client.post(f"/api/v1/pending/{pending[0]['id']}/answer", json={"content": "all"})
        results = await asyncio.gather(*calls)
        assert [r.status_code for r in results] == [200] * 4


async def test_a_waiter_that_times_out_cleans_up_after_itself(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, database_url=fresh_database_url(tmp_path)))
    service = app.state.service
    pid = await asyncio.to_thread(service.open_pending, canonical.from_openai(chat("timeout")))
    assert await service.wait_for_answer(pid, 0.05) is None
    assert service._waiters == {}
