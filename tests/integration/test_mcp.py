"""The MCP surface, driven by a real MCP client over streamable HTTP."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from tests.support.server import running_server

pytestmark = pytest.mark.integration

EXPECTED_TOOLS = {
    "get_status", "set_mode", "list_projects", "create_project", "get_project_settings",
    "update_project_settings", "list_pending", "wait_for_pending", "get_pending", "answer_pending",
    "list_entries",
    "get_entry", "upsert_entry", "update_entry", "delete_entry", "test_match",
    "list_calls", "log_stats", "entry_history", "list_candidates", "get_candidate",
    "approve_candidate", "reject_candidate",
}  # fmt: skip


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def call(client: Client, tool_name: str, /, **args: Any) -> Any:
    result = await client.call_tool(tool_name, args)
    return result.data if result.data is not None else json.loads(result.content[0].text)


def test_tools_are_listed_with_agent_steering_descriptions(tmp_path: Path) -> None:
    async def go(url: str) -> None:
        async with Client(url + "/mcp/") as client:
            tools = {t.name: t for t in await client.list_tools()}
            assert set(tools) == EXPECTED_TOOLS
            answer = tools["answer_pending"].description or ""
            for needle in ("{{now}}", "{{uuid}}", "tool_calls", "422", "param", "valid JSON"):
                assert needle in answer, f"answer_pending description lacks {needle!r}"
            assert "version" in (tools["update_entry"].description or "")
            assert "force" in (tools["update_entry"].description or "")
            assert "human" in (tools["delete_entry"].description or "")

    with running_server(tmp_path, mode="record") as url:
        run(go(url))


def test_an_agent_records_a_whole_session_using_only_mcp_tools(tmp_path: Path) -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": "Greet {{name}} via mcp"}]}
    held: dict[str, httpx.Response] = {}

    async def go(url: str) -> None:
        async with Client(url + "/mcp/") as client:
            status = await call(client, "get_status")
            assert status["mode"] == "record"
            await call(client, "create_project", id="mcp-app", name="MCP App")

            import threading

            def app_call() -> None:
                held["resp"] = httpx.post(
                    url + "/v1/projects/mcp-app/chat/completions", json=body, timeout=20
                )

            t = threading.Thread(target=app_call)
            t.start()
            pending: list[dict[str, Any]] = []
            for _ in range(200):
                pending = await call(client, "list_pending", project="mcp-app")
                if pending:
                    break
                await asyncio.sleep(0.02)
            detail = await call(client, "get_pending", id=pending[0]["id"])
            assert detail["request"]["messages"][-1]["content"] == "Greet {{name}} via mcp"

            with pytest.raises(ToolError) as bad:
                await client.call_tool(
                    "answer_pending", {"id": pending[0]["id"], "answer": {"content": 5}}
                )
            assert "message.content" in str(bad.value)

            await call(
                client,
                "answer_pending",
                id=pending[0]["id"],
                answer={"content": "Hello from the agent at {{now}}"},
            )
            t.join(20)

            match = await call(
                client, "test_match", project="mcp-app", prompt=body["messages"][0]["content"]
            )
            assert match["matched"] and match["would"] == "serve"
            entries = await call(client, "list_entries", project="mcp-app")
            assert entries["total"] == 1 and entries["entries"][0]["version"] == 1

    with running_server(tmp_path, mode="record", record_timeout=20.0) as url:
        run(go(url))
    assert held["resp"].status_code == 200
    assert (
        held["resp"]
        .json()["choices"][0]["message"]["content"]
        .startswith("Hello from the agent at 20")
    )


def test_pins_and_version_conflicts_are_enforced_for_agents(tmp_path: Path) -> None:
    async def go(url: str) -> None:
        async with Client(url + "/mcp/") as client:
            made = await call(
                client,
                "upsert_entry",
                project="default",
                request={"messages": [{"role": "user", "content": "pin me"}]},
                response={"content": "v1"},
            )
            assert made["created"] is True
            entry = made["entry"]
            # an agent can pin; a human edit is simulated through REST
            await call(client, "update_entry", id=entry["id"], version=1, changes={"pinned": True})
            with pytest.raises(ToolError) as pinned:
                await client.call_tool(
                    "update_entry",
                    {"id": entry["id"], "version": 2, "changes": {"response": {"content": "x"}}},
                )
            assert "entry_pinned" in str(pinned.value)
            with pytest.raises(ToolError) as stale:
                await client.call_tool(
                    "update_entry",
                    {"id": entry["id"], "version": 1, "changes": {"selection": "cycle"}},
                )
            assert "version_conflict" in str(stale.value)

    with running_server(tmp_path) as url:
        run(go(url))


def test_the_mcp_endpoint_honours_the_api_key(tmp_path: Path) -> None:
    async def go(url: str) -> None:
        with pytest.raises(Exception):  # noqa: B017, PT011  (401 surfaces as a transport error)
            async with Client(url + "/mcp/") as client:
                await client.list_tools()
        async with Client(url + "/mcp/", auth="topsecret") as client:
            assert {t.name for t in await client.list_tools()} == EXPECTED_TOOLS

    with running_server(tmp_path, api_key="topsecret") as url:
        run(go(url))
