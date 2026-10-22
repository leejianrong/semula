"""Two replicas on one Postgres share events and held-call wake-ups through LISTEN/NOTIFY
(ADR-0011 section 8, SEM-90). Needs a Postgres (``make test-postgres``); skips on SQLite."""

import asyncio
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from semula import canonical
from semula.config import Settings
from semula.db import Database
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("SEMULA_TEST_DATABASE_URL"), reason="LISTEN/NOTIFY is Postgres only"
    ),
]


def body(text: str) -> Any:
    return canonical.from_openai({"messages": [{"role": "user", "content": text}]})


class Replicas:
    def __init__(self, tmp_path: Path) -> None:
        url = fresh_database_url(tmp_path)
        channel = f"semula_{uuid.uuid4().hex[:10]}"
        self.a = Service(Database(url), Settings(data_dir=tmp_path))
        self.b = Service(Database(url), Settings(data_dir=tmp_path))  # a second process, in effect
        self.a.start_cluster(url, channel)
        self.b.start_cluster(url, channel)
        self.a.create_workspace("w")
        self.wa = self.a.for_scope(Scope("w"))
        self.wb = self.b.for_scope(Scope("w"))

    def stop(self) -> None:
        self.a.stop_cluster()
        self.b.stop_cluster()


@pytest.fixture
def replicas(tmp_path: Path) -> Iterator[Replicas]:
    r = Replicas(tmp_path)
    try:
        yield r
    finally:
        r.stop()


async def drain(sub: Any, wait: float = 0.6) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        try:
            out.append(await asyncio.wait_for(sub.get(), wait))
        except TimeoutError:
            return out


def test_an_event_on_one_replica_reaches_the_other_exactly_once(replicas: Replicas) -> None:
    async def go() -> None:
        with replicas.a.events.subscribe() as on_a, replicas.b.events.subscribe() as on_b:
            replicas.wa.create_project("shop")
            seen_b = await drain(on_b)
            seen_a = await drain(on_a)
        want = {"type": "projects", "workspace": "w"}
        assert seen_b.count(want) == 1, seen_b
        assert seen_a.count(want) == 1, "the sender must not hear its own event twice"

    asyncio.run(go())


def test_an_answer_on_one_replica_wakes_a_call_held_on_the_other_at_once(
    replicas: Replicas,
) -> None:
    replicas.wa.create_project("shop")
    pending = replicas.wa.open_pending(body("held on b"), "shop")

    async def go() -> float:
        held = asyncio.create_task(replicas.wb.wait_for_answer(pending, 30))
        await asyncio.sleep(0.3)
        started = time.monotonic()
        await asyncio.to_thread(replicas.wa.answer_pending, pending, {"content": "from a"})
        stored = await asyncio.wait_for(held, 5)
        assert stored["message"]["content"] == "from a"
        return time.monotonic() - started

    assert asyncio.run(go()) < 0.8  # a notification, not the one-second poll


def test_a_lost_notification_is_covered_by_the_poll(replicas: Replicas) -> None:
    replicas.wa.create_project("shop")
    pending = replicas.wa.open_pending(body("lossy"), "shop")
    replicas.a.events.forward = None  # replica a no longer tells anyone anything

    async def go() -> None:
        held = asyncio.create_task(replicas.wb.wait_for_answer(pending, 30))
        await asyncio.sleep(0.3)
        await asyncio.to_thread(replicas.wa.answer_pending, pending, {"content": "quietly"})
        stored = await asyncio.wait_for(held, 4)
        assert stored["message"]["content"] == "quietly"

    asyncio.run(go())


def test_a_waiter_for_pending_calls_on_one_replica_sees_a_park_on_the_other(
    replicas: Replicas,
) -> None:
    replicas.wa.create_project("shop")

    async def go() -> None:
        waiting = asyncio.create_task(replicas.wb.wait_for_pending("shop", 10))
        await asyncio.sleep(0.3)
        started = time.monotonic()
        await asyncio.to_thread(replicas.wa.open_pending, body("parked on a"), "shop")
        found = await asyncio.wait_for(waiting, 5)
        assert [p["last_message"] for p in found] == ["parked on a"]
        assert time.monotonic() - started < 0.8

    asyncio.run(go())


def test_publishing_never_waits_for_the_network(replicas: Replicas) -> None:
    replicas.wa.create_project("shop")
    started = time.perf_counter()
    for n in range(200):
        replicas.a.events.publish({"type": "call", "id": n, "workspace": "w"})
    assert time.perf_counter() - started < 0.25  # queued for a sender thread, not sent inline
