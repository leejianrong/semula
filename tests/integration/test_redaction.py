"""Secrets never reach the database; matching is unaffected (SEM-36)."""

import asyncio

import httpx
import pytest

from tests.integration.conftest import chat
from tests.integration.test_call_log import every_stored_value
from tests.integration.test_lexical_match import BASE, WEAK, ask
from tests.integration.test_replay import record_answer

pytestmark = pytest.mark.integration

SECRET = "ACME-7781-SECRET"  # noqa: S105
KEY = "sk-" + "ant-api03-AbCdEfGhIjKlMnOpQrStUvWx"  # noqa: S105


async def settings(client: httpx.AsyncClient, **values: object) -> httpx.Response:
    return await client.patch("/api/v1/projects/default", json={"settings": values})


async def test_a_listed_pattern_never_appears_in_the_database(record: httpx.AsyncClient) -> None:
    await settings(record, redact_patterns=[r"ACME-\d+-SECRET"])
    body = chat(f"deploy with {SECRET} now")
    call = asyncio.create_task(record.post("/v1/chat/completions", json=body))
    for _ in range(100):
        pending = (await record.get("/api/v1/pending")).json()["pending"]
        if pending:
            break
        await asyncio.sleep(0.01)
    assert SECRET not in every_stored_value(record)  # not even while parked
    shown = (await record.get(f"/api/v1/pending/{pending[0]['id']}")).json()
    assert shown["request"]["messages"][0]["content"] == "deploy with [REDACTED] now"
    await record.post(f"/api/v1/pending/{pending[0]['id']}/answer", json={"content": "ok"})
    assert (await call).status_code == 200
    assert SECRET not in every_stored_value(record)
    (entry,) = (await record.get("/api/v1/projects/default/entries")).json()["entries"]
    assert entry["last_message"] == "deploy with [REDACTED] now"


async def test_the_live_request_still_matches_exactly_and_other_secrets_match_too(
    record: httpx.AsyncClient,
) -> None:
    await settings(record, redact_patterns=[r"ACME-\d+-SECRET"])
    await record_answer(record, chat(f"deploy with {SECRET} now"), {"content": "ok"})
    same = await record.post("/v1/chat/completions", json=chat(f"deploy with {SECRET} now"))
    other = await record.post("/v1/chat/completions", json=chat("deploy with ACME-1-SECRET now"))
    assert same.status_code == other.status_code == 200
    db = record._transport.app.state.db  # type: ignore[attr-defined]
    tiers = [
        r["tier"] for r in db.query("SELECT tier FROM call_log WHERE outcome='hit' ORDER BY id")
    ]
    assert tiers == ["exact", "normalised"]  # redacting storage did not move the live fingerprint


async def test_common_credentials_are_redacted_by_default_and_can_be_left_alone(
    record: httpx.AsyncClient,
) -> None:
    await record_answer(record, chat(f"my key is {KEY}"), {"content": "noted"})
    assert KEY not in every_stored_value(record)
    assert (await settings(record, redact_defaults=False)).status_code == 200
    await record_answer(record, chat(f"another key {KEY}"), {"content": "noted"})
    assert KEY in every_stored_value(record)


async def test_a_new_pattern_cleans_what_was_stored_before_it(record: httpx.AsyncClient) -> None:
    await record_answer(record, chat(f"deploy with {SECRET} now"), {"content": "ok"})
    assert SECRET in every_stored_value(record)
    await settings(record, redact_patterns=[r"ACME-\d+-SECRET"])
    assert SECRET not in every_stored_value(record)
    resp = await record.post("/v1/chat/completions", json=chat(f"deploy with {SECRET} now"))
    assert resp.status_code == 200  # and it still matches


async def test_a_queued_near_miss_is_redacted_too(replay: httpx.AsyncClient) -> None:
    await settings(replay, redact_patterns=[r"def mul\(x, y\)"])
    await replay.post(
        "/api/v1/projects/default/entries",
        json={"request": ask(BASE), "response": {"content": "x"}},
    )
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    assert "def mul(x, y)" not in every_stored_value(replay)
    assert len((await replay.get("/api/v1/candidates")).json()["candidates"]) == 1


async def test_entries_written_through_the_api_are_redacted(replay: httpx.AsyncClient) -> None:
    resp = await replay.post(
        "/api/v1/projects/default/entries",
        json={"request": chat(f"key {KEY}"), "response": {"content": "ok"}},
    )
    assert resp.status_code == 201
    assert KEY not in every_stored_value(replay)
    hit = await replay.post("/v1/chat/completions", json=chat(f"key {KEY}"))
    assert hit.status_code == 200
