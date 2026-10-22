"""MCP on a hosted server: the token's workspace, project and scope bind every tool (SEM-87)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from semula import canonical
from semula.config import Settings
from semula.db import Database
from semula.mcp_server import TOOL_SCOPES
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url
from tests.support.server import running_server

pytestmark = pytest.mark.integration


def seed(tmp_path: Path) -> tuple[str, dict[str, Any]]:
    """A hosted database with workspace a (projects shop and other) and workspace b."""
    url = fresh_database_url(tmp_path)
    service = Service(Database(url), Settings(data_dir=tmp_path))
    for ws in ("a", "b"):
        service.create_workspace(ws)
    a = service.for_scope(Scope("a"))
    for project in ("shop", "other"):
        a.create_project(project)
    ids: dict[str, Any] = {}
    for project in ("shop", "other"):
        entry, _ = a.upsert_entry(
            project,
            {"messages": [{"role": "user", "content": f"{project} prompt"}]},
            {"content": project},
        )
        ids[f"{project}-entry"] = entry["id"]
        ids[f"{project}-pending"] = a.open_pending(
            canonical.from_openai({"messages": [{"role": "user", "content": f"{project} waits"}]}),
            project,
        )
    b = service.for_scope(Scope("b"))
    b.create_project("shop")
    ids["b-pending"] = b.open_pending(
        canonical.from_openai({"messages": [{"role": "user", "content": "b waits"}]}), "shop"
    )
    ids["tokens"] = {
        "replay": a.create_token("r", "replay")["token"],
        "record-shop": a.create_token("rs", "record", project="shop")["token"],
        "record-all": a.create_token("ra", "record")["token"],
        "admin-all": a.create_token("aa", "admin")["token"],
        "admin-shop": a.create_token("as", "admin", project="shop")["token"],
    }
    return url, ids


def client(url: str, token: str) -> Client:
    return Client(
        StreamableHttpTransport(url + "/mcp/", headers={"Authorization": f"Bearer {token}"})
    )


async def call(c: Client, tool: str, **args: Any) -> Any:
    result = await c.call_tool(tool, args)
    return result.data if result.data is not None else json.loads(result.content[0].text)


def error_code(exc: ToolError) -> str:
    return str(json.loads(str(exc))["error"]["code"])


def test_a_bound_record_token_sees_and_answers_only_its_own_project(tmp_path: Path) -> None:
    db, ids = seed(tmp_path)

    async def go(url: str) -> None:
        async with client(url, ids["tokens"]["record-shop"]) as c:
            tools = {t.name for t in await c.list_tools()}
            assert tools == set(TOOL_SCOPES), "every tool has a scope"
            assert [p["id"] for p in await call(c, "list_projects")] == ["shop"]
            status = await call(c, "get_status")
            assert [p["id"] for p in status["projects"]] == ["shop"]
            pending = await call(c, "list_pending")
            assert [p["id"] for p in pending] == [ids["shop-pending"]]
            waited = await call(c, "wait_for_pending", timeout_seconds=1)
            assert [p["id"] for p in waited] == [ids["shop-pending"]]
            for hidden in (ids["other-pending"], ids["b-pending"]):
                with pytest.raises(ToolError) as caught:
                    await call(c, "get_pending", id=hidden)
                assert error_code(caught.value) == "pending_not_found"
                with pytest.raises(ToolError):
                    await call(c, "answer_pending", id=hidden, answer={"content": "x"})
            with pytest.raises(ToolError) as caught:
                await call(c, "get_entry", id=ids["other-entry"])
            assert error_code(caught.value) == "entry_not_found"
            with pytest.raises(ToolError) as caught:
                await call(c, "list_entries", project="other")
            assert error_code(caught.value) == "project_not_found"
            answered = await call(
                c, "answer_pending", id=ids["shop-pending"], answer={"content": "hi"}
            )
            assert answered["pending_id"] == ids["shop-pending"]

    with running_server(tmp_path, hosted=True, database_url=db) as url:
        asyncio.run(go(url))


def test_a_record_token_cannot_use_the_tools_a_human_decides(tmp_path: Path) -> None:
    db, ids = seed(tmp_path)

    async def go(url: str) -> None:
        async with client(url, ids["tokens"]["record-all"]) as c:
            for tool, args in (
                ("delete_entry", {"id": ids["shop-entry"], "version": 1}),
                ("set_mode", {"mode": "record", "project": "shop"}),
                ("create_project", {"id": "new-one"}),
                ("update_project_settings", {"id": "shop", "settings": {"tail_n": 1}}),
                ("approve_candidate", {"id": "X"}),
                ("reject_candidate", {"id": "X"}),
                (
                    "update_entry",
                    {
                        "id": ids["shop-entry"],
                        "version": 1,
                        "changes": {"pinned": True},
                        "force": True,
                    },
                ),
            ):
                with pytest.raises(ToolError) as caught:
                    await call(c, tool, **args)
                assert error_code(caught.value) == "insufficient_scope", tool
            edited = await call(
                c,
                "update_entry",
                id=ids["shop-entry"],
                version=1,
                changes={"response": {"content": "ok"}},
            )
            assert edited["version"] == 2

    with running_server(tmp_path, hosted=True, database_url=db) as url:
        asyncio.run(go(url))
    service = Service(Database(db), Settings(data_dir=tmp_path))
    revisions = service.for_scope(Scope("a")).entry_history(ids["shop-entry"])["revisions"]
    assert revisions[0]["actor"] == "agent"


def test_an_admin_token_does_the_admin_work_in_its_own_workspace_and_project(
    tmp_path: Path,
) -> None:
    db, ids = seed(tmp_path)

    async def go(url: str) -> None:
        async with client(url, ids["tokens"]["admin-all"]) as c:
            projects = {p["id"] for p in await call(c, "list_projects")}
            assert projects == {"default", "shop", "other"}  # workspace a's, never b's
            await call(c, "set_mode", mode="record", project="shop")
            with pytest.raises(ToolError) as caught:
                await call(c, "set_mode", mode="record")  # the process default is the operator's
            assert error_code(caught.value) == "forbidden"
            await call(c, "delete_entry", id=ids["other-entry"], version=1)
        async with client(url, ids["tokens"]["admin-shop"]) as c:
            with pytest.raises(ToolError) as caught:
                await call(c, "delete_entry", id=ids["other-entry"], version=1)
            assert error_code(caught.value) in ("entry_not_found", "project_not_found")
            with pytest.raises(ToolError) as caught:
                await call(c, "create_project", id="another")
            assert error_code(caught.value) == "forbidden"

    with running_server(tmp_path, hosted=True, database_url=db) as url:
        asyncio.run(go(url))


def test_no_token_a_replay_token_and_a_browser_origin_cannot_use_mcp(tmp_path: Path) -> None:
    db, ids = seed(tmp_path)
    with running_server(tmp_path, hosted=True, database_url=db) as url:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        none = httpx.post(url + "/mcp/", json=body)
        assert none.status_code == 401
        replay = httpx.post(
            url + "/mcp/", json=body, headers={"Authorization": f"Bearer {ids['tokens']['replay']}"}
        )
        assert replay.status_code == 403 and replay.json()["error"]["code"] == "insufficient_scope"
        browser = httpx.post(
            url + "/mcp/", json=body,
            headers={"Authorization": f"Bearer {ids['tokens']['record-all']}", "Origin": "https://evil.example"},
        )  # fmt: skip
        assert (
            browser.status_code == 403 and browser.json()["error"]["code"] == "origin_not_allowed"
        )
