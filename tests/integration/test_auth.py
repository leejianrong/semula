import json
import threading
from pathlib import Path

import httpx
import pytest

from semula.cli import main
from tests.integration.conftest import chat, make_client
from tests.support.server import running_server

pytestmark = pytest.mark.integration

KEY = "s3cret-key"


async def test_everything_but_health_requires_the_key(tmp_path: Path) -> None:
    async with make_client(tmp_path, api_key=KEY) as client:
        assert (await client.get("/health")).status_code == 200
        for method, path in (
            ("POST", "/v1/chat/completions"),
            ("GET", "/v1/models"),
            ("GET", "/api/v1/projects"),
            ("GET", "/api/v1/pending"),
            ("GET", "/mcp/anything"),
        ):
            resp = await client.request(method, path, json=chat() if method == "POST" else None)
            assert resp.status_code == 401, path
            assert resp.json()["error"]["code"] == "invalid_api_key"
            assert resp.headers["www-authenticate"] == "Bearer"


async def test_bearer_and_x_api_key_both_work_and_wrong_keys_do_not(tmp_path: Path) -> None:
    async with make_client(tmp_path, api_key=KEY) as client:
        assert (
            await client.get("/v1/models", headers={"Authorization": f"Bearer {KEY}"})
        ).status_code == 200
        assert (await client.get("/v1/models", headers={"X-API-Key": KEY})).status_code == 200
        assert (
            await client.get("/v1/models", headers={"Authorization": "Bearer nope"})
        ).status_code == 401
        assert (await client.get("/v1/models", headers={"Authorization": KEY})).status_code == 401
        assert (await client.get("/v1/models", headers={"X-API-Key": ""})).status_code == 401


async def test_no_key_configured_means_open(tmp_path: Path) -> None:
    async with make_client(tmp_path) as client:
        assert (await client.get("/v1/models")).status_code == 200


async def test_the_secret_is_not_in_the_settings_repr(tmp_path: Path) -> None:
    from semula.config import Settings

    assert KEY not in repr(Settings(data_dir=tmp_path, api_key=KEY))


def test_cli_sends_the_key_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path, api_key=KEY) as url:
        monkeypatch.delenv("SEMULA_API_KEY", raising=False)
        assert main(["--url", url, "pending"]) == 1
        assert "invalid_api_key" in capsys.readouterr().err
        monkeypatch.setenv("SEMULA_API_KEY", KEY)
        assert main(["--url", url, "--json", "pending"]) == 0
        assert json.loads(capsys.readouterr().out) == []


def test_the_openai_sdk_style_bearer_key_works(tmp_path: Path) -> None:
    result: dict[str, int] = {}
    with running_server(tmp_path, api_key=KEY, mode="replay") as url:

        def call() -> None:
            resp = httpx.post(
                url + "/v1/chat/completions",
                json=chat(),
                headers={"Authorization": f"Bearer {KEY}"},
            )
            result["status"] = resp.status_code

        t = threading.Thread(target=call)
        t.start()
        t.join(10)
    assert result["status"] == 424  # authenticated, then a normal replay miss
