import asyncio
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import chat, make_client
from tests.integration.test_replay import record_answer

pytestmark = pytest.mark.integration


async def test_a_held_call_is_released_by_an_answer(record: httpx.AsyncClient) -> None:
    body = await record_answer(record, chat("park me"), {"content": "released"})
    assert body["choices"][0]["message"]["content"] == "released"


async def test_pending_detail_shows_the_request_the_agent_must_answer(
    record: httpx.AsyncClient,
) -> None:
    call = asyncio.create_task(record.post("/v1/chat/completions", json=chat("detail please")))
    await asyncio.sleep(0.05)
    listed = (await record.get("/api/v1/pending")).json()["pending"]
    detail = (await record.get(f"/api/v1/pending/{listed[0]['id']}")).json()
    assert detail["request"]["messages"][-1]["content"] == "detail please"
    await record.post(f"/api/v1/pending/{listed[0]['id']}/answer", json={"content": "x"})
    await call


async def test_identical_concurrent_misses_share_one_pending_row(record: httpx.AsyncClient) -> None:
    calls = [
        asyncio.create_task(record.post("/v1/chat/completions", json=chat("same")))
        for _ in range(3)
    ]
    pending: list[dict] = []
    for _ in range(200):  # wait for the first miss to park; a slow database can take a while
        pending = (await record.get("/api/v1/pending")).json()["pending"]
        if pending:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)  # let the other two arrive and join it
    pending = (await record.get("/api/v1/pending")).json()["pending"]
    assert len(pending) == 1
    await record.post(f"/api/v1/pending/{pending[0]['id']}/answer", json={"content": "once"})
    results = await asyncio.gather(*calls)
    assert [r.json()["choices"][0]["message"]["content"] for r in results] == ["once"] * 3


async def test_timeout_returns_an_error_and_leaves_the_row_pending(tmp_path: Path) -> None:
    async with make_client(tmp_path, mode="record", record_timeout=0.1) as client:
        resp = await client.post("/v1/chat/completions", json=chat("nobody home"))
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "semula_record_timeout"
        pending = (await client.get("/api/v1/pending")).json()["pending"]
        assert len(pending) == 1

        # A late answer is still saved, so the next identical call replays it.
        late = await client.post(
            f"/api/v1/pending/{pending[0]['id']}/answer", json={"content": "late but saved"}
        )
        assert late.status_code == 200
        again = await client.post("/v1/chat/completions", json=chat("nobody home"))
        assert again.json()["choices"][0]["message"]["content"] == "late but saved"


async def test_answering_twice_or_unknown_ids_are_clear_errors(record: httpx.AsyncClient) -> None:
    missing = await record.post("/api/v1/pending/NOPE/answer", json={"content": "x"})
    assert missing.status_code == 404
    call = asyncio.create_task(record.post("/v1/chat/completions", json=chat("twice")))
    await asyncio.sleep(0.05)
    pid = (await record.get("/api/v1/pending")).json()["pending"][0]["id"]
    await record.post(f"/api/v1/pending/{pid}/answer", json={"content": "one"})
    await call
    again = await record.post(f"/api/v1/pending/{pid}/answer", json={"content": "two"})
    assert again.status_code == 409


async def test_an_invalid_answer_is_rejected_and_the_call_stays_held(
    record: httpx.AsyncClient,
) -> None:
    call = asyncio.create_task(record.post("/v1/chat/completions", json=chat("validate me")))
    await asyncio.sleep(0.05)
    pid = (await record.get("/api/v1/pending")).json()["pending"][0]["id"]

    bad = await record.post(f"/api/v1/pending/{pid}/answer", json={"content": 5})
    assert bad.status_code == 422
    assert bad.json()["error"]["param"] == "message.content"
    assert not call.done()
    db = record._transport.app.state.db  # type: ignore[attr-defined]
    assert db.query("SELECT 1 FROM entry") == []

    good = await record.post(f"/api/v1/pending/{pid}/answer", json={"content": "fine"})
    assert good.status_code == 200
    assert (await call).json()["choices"][0]["message"]["content"] == "fine"


async def test_tool_call_answers_are_checked_against_the_request_tools(
    record: httpx.AsyncClient,
) -> None:
    tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
    call = asyncio.create_task(
        record.post("/v1/chat/completions", json=chat("weather?", tools=[tool]))
    )
    await asyncio.sleep(0.05)
    pid = (await record.get("/api/v1/pending")).json()["pending"][0]["id"]

    def answer(name: str, args: str) -> dict[str, object]:
        return {
            "message": {
                "content": None,
                "tool_calls": [{"function": {"name": name, "arguments": args}}],
            }
        }

    wrong_tool = await record.post(f"/api/v1/pending/{pid}/answer", json=answer("nope", "{}"))
    assert wrong_tool.status_code == 422
    bad_args = await record.post(
        f"/api/v1/pending/{pid}/answer", json=answer("get_weather", '{"city": 1}')
    )
    assert bad_args.status_code == 422
    assert bad_args.json()["error"]["param"].endswith("arguments.city")
    assert not call.done()

    good = await record.post(
        f"/api/v1/pending/{pid}/answer", json=answer("get_weather", '{"city": "KL"}')
    )
    assert good.status_code == 200
    message = (await call).json()["choices"][0]
    assert message["finish_reason"] == "tool_calls"
    assert message["message"]["tool_calls"][0]["function"]["arguments"] == '{"city": "KL"}'
