"""The dashboard is mounted at /ui and leaves everything else alone (SEM-46)."""

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import chat, make_client
from tests.support.server import running_server

pytestmark = pytest.mark.integration


async def test_ui_serves_the_indah_shell(replay: httpx.AsyncClient) -> None:
    resp = await replay.get("/ui/")
    assert resp.status_code == 200 and "text/html" in resp.headers["content-type"]
    assert "<title>semula" in resp.text


async def test_a_deep_link_loads_the_shell_too(replay: httpx.AsyncClient) -> None:
    resp = await replay.get("/ui/projects/default/entries/01J0000000000000000000000")
    assert resp.status_code == 200 and "<title>semula" in resp.text


async def test_the_root_goes_to_the_dashboard(replay: httpx.AsyncClient) -> None:
    resp = await replay.get("/")
    assert resp.status_code in (302, 307) and resp.headers["location"].endswith("ui/")


async def test_the_api_the_health_check_and_replay_are_unaffected(
    replay: httpx.AsyncClient,
) -> None:
    await replay.get("/ui/")  # build the dashboard first
    assert (await replay.get("/health")).json()["status"] == "ok"
    assert (await replay.get("/api/v1/projects")).status_code == 200
    miss = await replay.post("/v1/chat/completions", json=chat("anything"))
    assert miss.status_code == 424  # semula's own error, not the dashboard's 404
    assert (await replay.get("/v1/models")).status_code == 200


async def test_the_dashboard_can_be_switched_off(tmp_path: Path) -> None:
    async with make_client(tmp_path, mode="replay", ui=False) as client:
        assert (await client.get("/ui/")).status_code == 404
        assert (await client.get("/health")).status_code == 200


async def test_an_api_key_closes_the_dashboard_with_a_reason(tmp_path: Path) -> None:
    async with make_client(tmp_path, mode="replay", api_key="s3cret") as client:
        shut = await client.get("/ui/")
        assert shut.status_code == 403 and "SEMULA_API_KEY" in shut.text
        assert (await client.get("/ui/projects/default/log")).status_code == 403
        assert (await client.get("/api/v1/mode")).status_code == 401  # the API still needs the key
        ok = await client.get("/api/v1/mode", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


def test_importing_the_app_does_not_load_indah() -> None:
    """Start-up has no room for it: cold start is budgeted at 2 s and measured at 1.9."""
    code = "import sys, semula.app; sys.exit('indah' in sys.modules)"
    run = subprocess.run([sys.executable, "-c", code], check=False)  # noqa: S603 (our own code)
    assert run.returncode == 0


async def test_nothing_is_built_until_the_first_visit(tmp_path: Path) -> None:
    async with make_client(tmp_path, mode="replay") as client:
        app = client._transport.app  # type: ignore[attr-defined]
        dashboard = next(r.app for r in app.routes if getattr(r, "path", "") == "/ui")
        await client.get("/health")
        assert dashboard._app is None
        await client.get("/ui/")
        assert dashboard._app is not None


def test_the_dashboard_works_under_a_real_server(tmp_path: Path) -> None:
    with running_server(tmp_path, mode="replay") as url:
        assert httpx.get(url + "/ui/", timeout=20).status_code == 200
        assert httpx.get(url + "/mcp/", timeout=20).status_code != 404
        assert httpx.get(url + "/ui/projects/default/log", timeout=20).status_code == 200
