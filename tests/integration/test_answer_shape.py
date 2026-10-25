"""The pending view says what answer the request allows, on every surface (SEM-97)."""

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

from semula.cli import main
from tests.support.server import running_server

pytestmark = pytest.mark.integration

MOVE = {
    "type": "function",
    "function": {
        "name": "move",
        "description": "Move a piece",
        "parameters": {
            "type": "object",
            "properties": {"dir": {"enum": ["north", "south"]}, "steps": {"type": "integer"}},
            "required": ["dir"],
        },
    },
}
BODY = {
    "model": "m",
    "messages": [{"role": "user", "content": "what now"}],
    "tools": [MOVE],
    "max_tokens": 40,
}


def park(url: str) -> str:
    threading.Thread(
        target=lambda: httpx.post(url + "/v1/chat/completions", json=BODY, timeout=10), daemon=True
    ).start()
    for _ in range(200):
        items = httpx.get(url + "/api/v1/pending").json()["pending"]
        if items:
            return str(items[0]["id"])
        threading.Event().wait(0.02)
    raise AssertionError("nothing parked")


def check(shape: dict[str, Any]) -> None:
    move = shape["tools"][0]
    assert move["name"] == "move" and move["description"] == "Move a piece"
    assert move["parameters"] == MOVE["function"]["parameters"]
    call = move["skeleton"]["message"]["tool_calls"][0]["function"]
    assert call == {"name": "move", "arguments": {"dir": "north"}}
    assert shape["max_tokens"] == 40


def test_rest_cli_and_mcp_show_the_shape_and_the_skeleton_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path, mode="record", record_timeout=10.0) as url:
        pid = park(url)
        shown = httpx.get(f"{url}/api/v1/pending/{pid}").json()
        check(shown["shape"])
        assert (
            "shape" not in httpx.get(url + "/api/v1/pending").json()["pending"][0]
        )  # list stays small

        assert main(["--url", url, "--json", "show", pid]) == 0
        check(json.loads(capsys.readouterr().out)["shape"])

        async def via_mcp() -> dict[str, Any]:
            async with Client(url + "/mcp/") as client:
                result = await client.call_tool("get_pending", {"id": pid})
                return dict(
                    result.data if result.data is not None else json.loads(result.content[0].text)
                )

        check(asyncio.run(via_mcp())["shape"])

        skeleton = shown["shape"]["tools"][0][
            "skeleton"
        ]  # sent back unchanged, it is a valid answer
        assert httpx.post(f"{url}/api/v1/pending/{pid}/answer", json=skeleton).status_code == 200
