import asyncio

import httpx
import pytest

from tests.integration.conftest import chat, make_client
from tests.integration.test_replay import record_answer

pytestmark = pytest.mark.integration


async def patch(client: httpx.AsyncClient, project: str, settings: dict) -> httpx.Response:
    return await client.patch(f"/api/v1/projects/{project}", json={"settings": settings})


async def test_defaults_are_visible_and_documented_by_the_api(replay: httpx.AsyncClient) -> None:
    settings = (await replay.get("/api/v1/projects/default")).json()["settings"]
    assert settings == {
        "mode": None,
        "models": None,
        "tail_n": None,
        "scrub_patterns": [],
        "redact_patterns": [],
        "redact_defaults": True,
        "include_params": [],
        "record_timeout": None,
        "bm25_serve_threshold": 1.0,
        "bm25_review_threshold": 0.6,
        "embedding_review_threshold": 0.75,
        "embedding_serve_threshold": None,
    }


async def test_a_setting_changes_matching_and_defaults_stay_unchanged(
    record: httpx.AsyncClient,
) -> None:
    await record_answer(record, chat("same words", temperature=0.1), {"content": "cool"})
    hot = chat("same words", temperature=0.9)
    # default: temperature is ignored, so this hits
    assert (await record.post("/v1/chat/completions", json=hot)).status_code == 200

    assert (await patch(record, "default", {"include_params": ["temperature"]})).status_code == 200
    # recorded temperature 0.1 now differs from 0.9: the call parks instead of hitting
    call = asyncio.create_task(record.post("/v1/chat/completions", json=hot))
    parked = await record.get("/api/v1/pending", params={"wait": 10})  # waits for the park
    assert not call.done()
    pid = parked.json()["pending"][0]["id"]
    await record.post(f"/api/v1/pending/{pid}/answer", json={"content": "hot"})
    assert (await call).json()["choices"][0]["message"]["content"] == "hot"
    # the original temperature still hits its own entry (entries were re-keyed)
    again = await record.post("/v1/chat/completions", json=chat("same words", temperature=0.1))
    assert again.json()["choices"][0]["message"]["content"] == "cool"


async def test_tail_n_ignores_noisy_history(record: httpx.AsyncClient) -> None:
    def convo(noise: str) -> dict:
        return {
            "model": "m",
            "messages": [
                {"role": "system", "content": noise},
                {"role": "user", "content": "the real question"},
            ],
        }

    await patch(record, "default", {"tail_n": 1})
    await record_answer(record, convo("noise one"), {"content": "answered"})
    resp = await record.post("/v1/chat/completions", json=convo("totally different noise"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "answered"


async def test_scrub_patterns_apply_and_changing_them_rekeys_old_entries(
    record: httpx.AsyncClient,
) -> None:
    await record_answer(record, chat("see ticket-12 please"), {"content": "done"})
    # before the rule: ticket-99 is a different prompt, so it would park
    await patch(record, "default", {"scrub_patterns": [r"ticket-\d+"]})
    resp = await record.post("/v1/chat/completions", json=chat("see ticket-99 please"))
    assert resp.status_code == 200 and resp.json()["choices"][0]["message"]["content"] == "done"


async def test_null_resets_a_setting_to_its_default(replay: httpx.AsyncClient) -> None:
    await patch(replay, "default", {"tail_n": 3, "models": ["m1"]})
    got = (await patch(replay, "default", {"tail_n": None})).json()["settings"]
    assert got["tail_n"] is None and got["models"] == ["m1"]


@pytest.mark.parametrize(
    ("settings", "param"),
    [
        ({"tail_n": 0}, "settings.tail_n"),
        ({"scrub_patterns": ["("]}, "settings.scrub_patterns"),
        ({"redact_patterns": ["("]}, "settings.redact_patterns"),
        ({"include_params": ["colour"]}, "settings.include_params.0"),
        ({"record_timeout": -1}, "settings.record_timeout"),
        ({"nope": 1}, "settings.nope"),
    ],
)
async def test_invalid_settings_are_rejected_with_a_param(
    replay: httpx.AsyncClient, settings: dict, param: str
) -> None:
    resp = await patch(replay, "default", settings)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_settings"
    assert resp.json()["error"]["param"] == param


async def test_record_timeout_can_be_set_per_project(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async with make_client(tmp_path, mode="record", record_timeout=60.0) as client:
        await patch(client, "default", {"record_timeout": 0.1})
        resp = await client.post("/v1/chat/completions", json=chat("quick timeout"))
        assert resp.status_code == 504
