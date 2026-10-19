import asyncio
import json

import httpx
import pytest

from tests.integration.conftest import chat
from tests.integration.test_replay import record_answer

pytestmark = pytest.mark.integration


async def test_project_crud(replay: httpx.AsyncClient) -> None:
    listed = (await replay.get("/api/v1/projects")).json()["projects"]
    assert [p["id"] for p in listed] == ["default"]

    created = await replay.post("/api/v1/projects", json={"id": "auth-service", "name": "Auth"})
    assert created.status_code == 201
    assert created.json()["name"] == "Auth" and created.json()["entries"] == 0

    assert (await replay.post("/api/v1/projects", json={"id": "auth-service"})).status_code == 409
    renamed = await replay.patch("/api/v1/projects/auth-service", json={"name": "Auth v2"})
    assert renamed.json()["name"] == "Auth v2"
    assert (await replay.get("/api/v1/projects/nope")).status_code == 404

    gone = await replay.delete("/api/v1/projects/auth-service")
    assert gone.status_code == 204
    assert (await replay.get("/api/v1/projects/auth-service")).status_code == 404


@pytest.mark.parametrize("bad", ["", "Has Caps", "-lead", "x" * 64, "a/b"])
async def test_bad_project_ids_are_rejected(replay: httpx.AsyncClient, bad: str) -> None:
    resp = await replay.post("/api/v1/projects", json={"id": bad})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_project_id"


async def test_the_default_project_cannot_be_deleted(replay: httpx.AsyncClient) -> None:
    resp = await replay.delete("/api/v1/projects/default")
    assert resp.status_code == 409


async def test_deleting_a_project_removes_its_entries_and_log(record: httpx.AsyncClient) -> None:
    await record.post("/api/v1/projects", json={"id": "doomed"})
    await record_answer(record, chat("belongs to doomed"), {"content": "x"}, project="doomed")
    db = record._transport.app.state.db  # type: ignore[attr-defined]
    assert db.query("SELECT 1 FROM entry WHERE project_id='doomed'")
    await record.delete("/api/v1/projects/doomed")
    assert db.query("SELECT 1 FROM entry WHERE project_id='doomed'") == []
    assert db.query("SELECT 1 FROM call_log WHERE project_id='doomed'") == []


async def test_project_counts_reflect_entries_and_pending(record: httpx.AsyncClient) -> None:
    await record.post("/api/v1/projects", json={"id": "counted"})
    await record_answer(record, chat("one"), {"content": "1"}, project="counted")
    call = asyncio.create_task(
        record.post(
            "/v1/chat/completions", json=chat("two"), headers={"X-Semula-Project": "counted"}
        )
    )
    await asyncio.sleep(0.05)
    row = (await record.get("/api/v1/projects/counted")).json()
    assert row["entries"] == 1 and row["pending"] == 1
    pid = (await record.get("/api/v1/pending", params={"project": "counted"})).json()["pending"][0]
    await record.post(f"/api/v1/pending/{pid['id']}/answer", json={"content": "2"})
    await call


# --- routing (SEM-17) ---------------------------------------------------------------


async def test_header_and_path_reach_the_same_project(record: httpx.AsyncClient) -> None:
    await record.post("/api/v1/projects", json={"id": "alpha"})
    await record_answer(record, chat("routed"), {"content": "alpha answer"}, project="alpha")

    by_header = await record.post(
        "/v1/chat/completions", json=chat("routed"), headers={"X-Semula-Project": "alpha"}
    )
    by_path = await record.post("/v1/projects/alpha/chat/completions", json=chat("routed"))
    for resp in (by_header, by_path):
        assert resp.json()["choices"][0]["message"]["content"] == "alpha answer"


async def test_projects_are_isolated_from_each_other(record: httpx.AsyncClient) -> None:
    from semula import canonical

    await record.post("/api/v1/projects", json={"id": "alpha"})
    await record.post("/api/v1/projects", json={"id": "beta"})
    await record_answer(record, chat("only alpha"), {"content": "a"}, project="alpha")
    service = record._transport.app.state.service  # type: ignore[attr-defined]
    req = canonical.from_openai(chat("only alpha"))
    assert service.find(req, "alpha") is not None
    assert service.find(req, "beta") is None
    assert service.find(req, "default") is None


async def test_path_wins_over_header(record: httpx.AsyncClient) -> None:
    await record.post("/api/v1/projects", json={"id": "alpha"})
    await record_answer(record, chat("who wins"), {"content": "from alpha"}, project="alpha")
    resp = await record.post(
        "/v1/projects/alpha/chat/completions",
        json=chat("who wins"),
        headers={"X-Semula-Project": "does-not-exist"},
    )
    assert resp.status_code == 200


async def test_unknown_project_is_a_clear_404_not_a_fallback(replay: httpx.AsyncClient) -> None:
    for resp in (
        await replay.post("/v1/projects/ghost/chat/completions", json=chat()),
        await replay.post(
            "/v1/chat/completions", json=chat(), headers={"X-Semula-Project": "ghost"}
        ),
        await replay.get("/v1/projects/ghost/models"),
    ):
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "project_not_found"


async def test_models_are_per_project(replay: httpx.AsyncClient) -> None:
    await replay.post("/api/v1/projects", json={"id": "alpha"})
    db = replay._transport.app.state.db  # type: ignore[attr-defined]
    db.run(
        "UPDATE project SET settings=:s WHERE id='alpha'", {"s": json.dumps({"models": ["m-a"]})}
    )
    alpha = (await replay.get("/v1/projects/alpha/models")).json()["data"]
    default = (await replay.get("/v1/models")).json()["data"]
    assert [m["id"] for m in alpha] == ["m-a"] and [m["id"] for m in default] == ["semula"]
