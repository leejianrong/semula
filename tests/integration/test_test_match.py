import httpx
import pytest

from tests.integration.conftest import chat
from tests.integration.test_replay import record_answer

pytestmark = pytest.mark.integration


async def match(client: httpx.AsyncClient, body: dict, project: str = "default") -> dict:
    resp = await client.post(f"/api/v1/projects/{project}/test-match", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def test_a_hit_reports_tier_entry_and_preview(record: httpx.AsyncClient) -> None:
    await record_answer(record, chat("What is the capital of France?"), {"content": "Paris."})
    out = await match(record, {"prompt": "What is the capital of France?"})
    assert out["matched"] and out["tier"] == "exact" and out["would"] == "serve"
    assert out["response_preview"] == "Paris." and out["entry_id"]
    assert out["nearest"] is None


async def test_normalised_tier_is_reported_for_a_noisy_prompt(record: httpx.AsyncClient) -> None:
    await record_answer(record, chat("Status at 2026-10-19T10:00:00Z?"), {"content": "ok"})
    out = await match(record, {"prompt": "Status at 2026-12-01T09:00:00Z?"})
    assert out["tier"] == "normalised"


async def test_a_miss_names_the_nearest_candidate_and_what_would_happen(
    record: httpx.AsyncClient, replay: httpx.AsyncClient
) -> None:
    await record_answer(record, chat("What is the capital of France?"), {"content": "Paris."})
    miss = await match(record, {"prompt": "What is the capital of Spain?"})
    assert miss["matched"] is False and miss["would"] == "park"
    assert miss["nearest"]["last_message"] == "What is the capital of France?"
    assert 0 < miss["nearest"]["score"] < 1
    nothing = await match(replay, {"prompt": "anything"})
    assert nothing["would"] == "no_match_error" and nothing["nearest"] is None


async def test_test_match_has_no_side_effects(record: httpx.AsyncClient) -> None:
    await record_answer(record, chat("quiet"), {"content": "x"})
    db = record._transport.app.state.db  # type: ignore[attr-defined]
    before = db.query_one("SELECT COUNT(*) AS n FROM call_log")["n"]
    for _ in range(3):
        await match(record, {"prompt": "quiet"})
    assert db.query_one("SELECT COUNT(*) AS n FROM call_log")["n"] == before
    assert db.query_one("SELECT hit_count FROM entry")["hit_count"] == 0


async def test_full_request_bodies_and_bad_input(record: httpx.AsyncClient) -> None:
    await record_answer(record, chat("full body"), {"content": "x"})
    assert (await match(record, {"request": chat("full body", temperature=0.2)}))["matched"]
    assert (await record.post("/api/v1/projects/default/test-match", json={})).status_code == 400
    assert (
        await record.post("/api/v1/projects/ghost/test-match", json={"prompt": "x"})
    ).status_code == 404


async def test_a_replay_miss_error_carries_the_nearest_candidate(
    record: httpx.AsyncClient,
) -> None:
    await record_answer(record, chat("Summarise the quarterly report"), {"content": "x"})
    await record.put("/api/v1/mode", json={"mode": "replay"})
    resp = await record.post("/v1/chat/completions", json=chat("Summarise the annual report"))
    assert resp.status_code == 424
    assert resp.json()["nearest"]["last_message"] == "Summarise the quarterly report"


async def test_mode_can_be_switched_at_runtime(replay: httpx.AsyncClient) -> None:
    assert (await replay.get("/api/v1/mode")).json() == {
        "mode": "replay",
        "projects": {"default": "replay"},
    }
    assert (await replay.put("/api/v1/mode", json={"mode": "record"})).json() == {
        "mode": "record",
        "persisted": False,
        "project": None,
    }
    assert (await replay.get("/health")).json()["mode"] == "record"
    assert (await replay.put("/api/v1/mode", json={"mode": "chaos"})).status_code == 422
