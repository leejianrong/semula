import asyncio

import httpx
import pytest

from tests.integration.conftest import chat

pytestmark = pytest.mark.integration


async def record_answer(client: httpx.AsyncClient, body: dict[str, object], answer: object) -> dict:
    """Send a call in record mode, answer it through the admin API, return the completion."""
    call = asyncio.create_task(client.post("/v1/chat/completions", json=body))
    for _ in range(100):
        pending = (await client.get("/api/v1/pending")).json()["pending"]
        if pending:
            break
        await asyncio.sleep(0.01)
    assert pending, "the call was never parked"
    resp = await client.post(f"/api/v1/pending/{pending[0]['id']}/answer", json=answer)
    assert resp.status_code == 200, resp.text
    done = await call
    assert done.status_code == 200, done.text
    return done.json()  # type: ignore[no-any-return]


async def test_replay_miss_returns_an_openai_shaped_error(replay: httpx.AsyncClient) -> None:
    resp = await replay.post("/v1/chat/completions", json=chat())
    assert resp.status_code == 424
    err = resp.json()["error"]
    assert err["code"] == "semula_no_match"
    assert {"message", "type", "param", "code"} <= err.keys()


async def test_recorded_answer_replays_through_the_openai_sdk_shape(
    record: httpx.AsyncClient,
) -> None:
    first = await record_answer(record, chat("What is 2+2?"), {"content": "4"})
    assert first["choices"][0]["message"] == {"role": "assistant", "content": "4"}

    # The same prompt is now a hit, even with a different model name and a timestamp.
    second = await record.post(
        "/v1/chat/completions", json={**chat("What is 2+2?"), "model": "another-model"}
    )
    body = second.json()
    assert second.status_code == 200
    assert body["object"] == "chat.completion"
    assert body["model"] == "another-model"
    assert body["choices"][0]["message"]["content"] == "4"
    assert body["id"] != first["id"]


async def test_hit_count_and_call_log_are_updated(record: httpx.AsyncClient) -> None:
    await record_answer(record, chat("count me"), {"content": "ok"})
    for _ in range(2):
        await record.post("/v1/chat/completions", json=chat("count me"))
    db = record._transport.app.state.db  # type: ignore[attr-defined]
    assert db.query_one("SELECT hit_count FROM entry")["hit_count"] == 2
    outcomes = [r["outcome"] for r in db.query("SELECT outcome FROM call_log ORDER BY id")]
    assert outcomes == ["recorded", "hit", "hit"]


async def test_normalised_tier_serves_a_prompt_that_differs_only_by_timestamp(
    record: httpx.AsyncClient,
) -> None:
    await record_answer(record, chat("Status at 2026-10-19T10:00:00Z?"), {"content": "fine"})
    resp = await record.post("/v1/chat/completions", json=chat("Status at 2026-10-20T11:30:00Z?"))
    assert resp.status_code == 200
    db = record._transport.app.state.db  # type: ignore[attr-defined]
    assert db.query_one("SELECT tier FROM call_log WHERE outcome='hit'")["tier"] == "normalised"


async def test_bad_requests_are_400s(replay: httpx.AsyncClient) -> None:
    bad_json = await replay.post(
        "/v1/chat/completions", content=b"{nope", headers={"content-type": "application/json"}
    )
    assert bad_json.status_code == 400 and bad_json.json()["error"]["code"] == "invalid_json"
    no_messages = await replay.post("/v1/chat/completions", json={"model": "x"})
    assert no_messages.status_code == 400


async def test_streaming_is_reported_as_not_implemented_yet(replay: httpx.AsyncClient) -> None:
    resp = await replay.post("/v1/chat/completions", json=chat(stream=True))
    assert resp.status_code == 501
