"""What the service tells live views besides calls: queues changed, mode changed (SEM-47)."""

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from tests.integration.conftest import chat
from tests.integration.test_call_log import stream_lines
from tests.integration.test_lexical_match import WEAK, ask, seed
from tests.support.server import running_server

pytestmark = pytest.mark.integration


def service_of(client: httpx.AsyncClient) -> Any:
    return client._transport.app.state.service  # type: ignore[attr-defined]


async def drain(sub: Any, wait: float = 0.3) -> list[dict]:
    """Every event that arrives within ``wait`` seconds of quiet."""
    out: list[dict] = []
    while True:
        try:
            out.append(await asyncio.wait_for(sub.get(), wait))
        except TimeoutError:
            return out


@pytest.fixture
async def events(record: httpx.AsyncClient) -> AsyncIterator[Any]:
    with service_of(record).events.subscribe() as sub:
        yield sub


async def park(client: httpx.AsyncClient, prompt: str) -> tuple[asyncio.Task, str]:
    call = asyncio.create_task(client.post("/v1/chat/completions", json=chat(prompt)))
    for _ in range(300):
        pending = (await client.get("/api/v1/pending")).json()["pending"]
        if pending:
            return call, pending[0]["id"]
        await asyncio.sleep(0.01)
    raise AssertionError("never parked")


async def test_parking_and_answering_a_call_say_the_queues_changed(
    record: httpx.AsyncClient, events: Any
) -> None:
    call, pending_id = await park(record, "wait for me")
    assert {"type": "queues", "project": "default"} in await drain(events)
    await record.post(f"/api/v1/pending/{pending_id}/answer", json={"content": "ok"})
    await call
    seen = await drain(events)
    assert {"type": "queues", "project": "default"} in seen
    assert any(e["type"] == "call" and e["outcome"] == "recorded" for e in seen)


async def test_a_queued_candidate_and_its_decision_say_the_queues_changed(
    replay: httpx.AsyncClient,
) -> None:
    await seed(replay)
    with service_of(replay).events.subscribe() as sub:
        await replay.post("/v1/chat/completions", json=ask(WEAK))
        assert {"type": "queues", "project": "default"} in await drain(sub)
        (found,) = (await replay.get("/api/v1/candidates")).json()["candidates"]
        await replay.post(f"/api/v1/candidates/{found['id']}/reject")
        assert {"type": "queues", "project": "default"} in await drain(sub)


async def test_authoring_an_answer_clears_what_it_supersedes(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    with service_of(replay).events.subscribe() as sub:
        resp = await replay.post(
            "/api/v1/projects/default/entries",
            json={"request": ask(WEAK), "response": {"content": "its own answer"}},
        )
        assert resp.status_code == 201
        assert {"type": "queues", "project": "default"} in await drain(sub)


async def test_switching_the_mode_is_announced(replay: httpx.AsyncClient) -> None:
    with service_of(replay).events.subscribe() as sub:
        await replay.put("/api/v1/mode", json={"mode": "record"})
        assert {"type": "mode", "project": None, "mode": "record"} in await drain(sub)


async def test_queue_counts(record: httpx.AsyncClient) -> None:
    service = service_of(record)
    assert service.queue_counts() == {"pending": 0, "review": 0}
    await record.post("/api/v1/projects", json={"id": "other"})
    c1, _ = await park(record, "one")
    c2 = asyncio.create_task(record.post("/v1/projects/other/chat/completions", json=chat("two")))
    for _ in range(300):
        if service.queue_counts("other")["pending"]:
            break
        await asyncio.sleep(0.01)
    assert service.queue_counts() == {"pending": 2, "review": 0}
    assert service.queue_counts("default")["pending"] == 1
    assert service.queue_counts("other")["pending"] == 1
    c1.cancel()
    c2.cancel()
    with pytest.raises(Exception, match="ghost"):
        service.queue_counts("ghost")


def test_the_public_stream_still_carries_only_calls(tmp_path: Any) -> None:
    """The dashboard hears more than ``/api/v1/log/stream`` does: its contract stays calls."""
    with running_server(tmp_path, mode="record", record_timeout=0.3) as url:
        lines: list[str] = []
        ready = threading.Event()
        t = threading.Thread(target=stream_lines, args=(url, "/api/v1/log/stream", lines, ready))
        t.start()
        assert ready.wait(5)
        httpx.post(url + "/v1/chat/completions", json=chat("parks, then times out"), timeout=5)
        t.join(5)
        assert [x for x in lines if x.startswith("event:")] == ["event: call"]


async def test_every_entry_write_says_the_entries_changed(
    record: httpx.AsyncClient, events: Any
) -> None:
    moved = {"type": "entries", "project": "default"}
    body = {"request": {"messages": [{"role": "user", "content": "events"}]}}

    created = await record.post(
        "/api/v1/projects/default/entries", json={**body, "response": {"content": "a"}}
    )
    assert moved in await drain(events), "creating an entry"

    entry = created.json()
    edited = await record.put(
        f"/api/v1/entries/{entry['id']}", json={"version": 1, "response": {"content": "b"}}
    )
    assert moved in await drain(events), "editing an entry"

    await record.delete(
        f"/api/v1/entries/{entry['id']}", params={"version": edited.json()["version"]}
    )
    assert moved in await drain(events), "deleting an entry"

    call, pending_id = await park(record, "answered into an entry")
    await drain(events)
    await record.post(f"/api/v1/pending/{pending_id}/answer", json={"content": "ok"})
    await call
    assert moved in await drain(events), "recording an answer"
