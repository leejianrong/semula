import json

import httpx
import pytest

from tests.integration.conftest import chat

pytestmark = pytest.mark.integration

ERROR_KEYS = {"message", "type", "param", "code"}


async def test_models_list_and_retrieve(replay: httpx.AsyncClient) -> None:
    listed = (await replay.get("/v1/models")).json()
    assert listed["object"] == "list" and listed["data"][0]["id"] == "semula"
    assert listed["data"][0]["object"] == "model"
    assert (await replay.get("/v1/models/semula")).json()["id"] == "semula"


async def test_models_come_from_the_project_settings(replay: httpx.AsyncClient) -> None:
    db = replay._transport.app.state.db  # type: ignore[attr-defined]
    db.execute("UPDATE project SET settings=?", (json.dumps({"models": ["gpt-4o", "claude-x"]}),))
    ids = [m["id"] for m in (await replay.get("/v1/models")).json()["data"]]
    assert ids == ["gpt-4o", "claude-x"]


async def test_unknown_model_is_an_openai_shaped_404(replay: httpx.AsyncClient) -> None:
    resp = await replay.get("/v1/models/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/v1/nothing", 404),
        ("GET", "/v1/chat/completions", 405),
        ("POST", "/v1/models", 405),
        ("GET", "/api/v1/pending/NOPE", 404),
    ],
)
async def test_every_error_uses_the_openai_envelope(
    replay: httpx.AsyncClient, method: str, path: str, status: int
) -> None:
    resp = await replay.request(method, path)
    assert resp.status_code == status
    assert resp.json()["error"].keys() >= ERROR_KEYS


async def test_semula_errors_are_in_the_envelope_too(replay: httpx.AsyncClient) -> None:
    resp = await replay.post("/v1/chat/completions", json=chat())
    assert resp.json()["error"].keys() >= ERROR_KEYS
