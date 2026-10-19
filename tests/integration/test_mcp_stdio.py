"""`semula mcp`: a stdio MCP server that forwards to a running semula."""

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from tests.support.server import running_server

pytestmark = pytest.mark.integration


def stdio_client(url: str, env: dict[str, str] | None = None) -> Client:
    transport = StdioTransport(
        command=sys.executable, args=["-m", "semula", "--url", url, "mcp"], env=env
    )
    return Client(transport)


def test_the_stdio_shim_lists_tools_and_forwards_calls(tmp_path: Path) -> None:
    async def go(url: str) -> Any:
        async with stdio_client(url) as client:
            names = {t.name for t in await client.list_tools()}
            status = await client.call_tool("get_status", {})
            return names, status.data

    with running_server(tmp_path, mode="record") as url:
        names, status = asyncio.run(go(url))
    assert {"answer_pending", "list_pending", "test_match"} <= names
    assert status["mode"] == "record"


def test_the_stdio_shim_passes_the_api_key(tmp_path: Path) -> None:
    async def go(url: str) -> Any:
        async with stdio_client(url, env={"SEMULA_API_KEY": "k3y", "PATH": ""}) as client:
            return (await client.call_tool("get_status", {})).data["version"]

    with running_server(tmp_path, api_key="k3y") as url:
        assert asyncio.run(go(url))


def test_the_shim_exits_non_zero_when_the_server_is_down_or_the_key_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from semula.cli import main

    assert main(["--url", "http://127.0.0.1:9", "mcp"]) == 1
    assert "cannot reach semula" in capsys.readouterr().err
    with running_server(tmp_path, api_key="right") as url:
        monkeypatch.setenv("SEMULA_API_KEY", "wrong")
        assert main(["--url", url, "mcp"]) == 5
