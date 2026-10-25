"""A ceiling on misses per run (SEM-96): a loop in the app cannot park hundreds of calls."""

from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import chat, make_client

pytestmark = pytest.mark.integration


async def ceiling(client: httpx.AsyncClient, n: int | None) -> None:
    resp = await client.patch("/api/v1/projects/default", json={"settings": {"max_misses": n}})
    assert resp.status_code == 200, resp.text


async def miss(client: httpx.AsyncClient, text: str) -> httpx.Response:
    return await client.post("/v1/chat/completions", json=chat(text))


async def answer_all(client: httpx.AsyncClient) -> None:
    for item in (await client.get("/api/v1/pending")).json()["pending"]:
        resp = await client.post(f"/api/v1/pending/{item['id']}/answer", json={"content": "ok"})
        assert resp.status_code == 200, resp.text


@pytest.fixture
async def record(tmp_path: Path):  # type: ignore[no-untyped-def]
    async with make_client(tmp_path, mode="record", record_timeout=0.05) as client:
        yield client


async def test_the_ceiling_stops_parking_and_says_why(record: httpx.AsyncClient) -> None:
    await ceiling(record, 2)
    for n in range(2):
        assert (await miss(record, f"q{n}")).status_code == 504  # parked, nobody answered
    refused = await miss(record, "q2")
    assert refused.status_code == 429
    error = refused.json()["error"]
    assert error["code"] == "semula_miss_ceiling"
    assert "max_misses" in error["message"] and "2" in error["message"]
    pending = (await record.get("/api/v1/pending")).json()["pending"]
    assert len(pending) == 2  # the third was not parked


async def test_a_call_that_joins_a_parked_one_is_not_a_new_miss(record: httpx.AsyncClient) -> None:
    await ceiling(record, 1)
    assert (await miss(record, "same")).status_code == 504
    assert (await miss(record, "same")).status_code == 504  # joins, still under the ceiling


async def test_answered_misses_still_count_and_hits_never_do(record: httpx.AsyncClient) -> None:
    await ceiling(record, 1)
    await miss(record, "one")
    await answer_all(record)
    for _ in range(5):
        hit = await miss(record, "one")
        assert hit.status_code == 200  # replay is free and uncounted
    assert (await miss(record, "two")).status_code == 429  # the one answered miss used the run


async def test_setting_the_ceiling_again_starts_a_new_run(record: httpx.AsyncClient) -> None:
    await ceiling(record, 1)
    await miss(record, "a")
    assert (await miss(record, "b")).status_code == 429
    await ceiling(record, 2)
    assert (await miss(record, "b")).status_code == 504
    assert (await miss(record, "c")).status_code == 504
    assert (await miss(record, "d")).status_code == 429  # a was last run's
    await ceiling(record, None)  # unset: no ceiling
    assert (await miss(record, "d")).status_code == 504


async def test_no_ceiling_by_default_and_replay_mode_is_untouched(
    record: httpx.AsyncClient,
) -> None:
    for n in range(5):
        assert (await miss(record, f"free{n}")).status_code == 504


async def test_a_miss_over_the_ceiling_is_logged_as_a_miss(record: httpx.AsyncClient) -> None:
    await ceiling(record, 0)
    assert (await miss(record, "x")).status_code == 429
    calls = (await record.get("/api/v1/log")).json()["calls"]
    assert calls[0]["outcome"] == "miss"
