"""The review queue through the CLI and MCP, matching REST (ADR-0005)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

from semula.cli import main
from tests.integration.test_lexical_match import BASE, WEAK, ask
from tests.support.server import running_server

pytestmark = pytest.mark.integration


def queue_one(url: str) -> dict[str, Any]:
    """Record an entry, then miss it narrowly in replay mode so a candidate is queued."""
    seeded = httpx.post(
        url + "/api/v1/projects/default/entries",
        json={"request": ask(BASE), "response": {"content": "answer"}},
    )
    assert seeded.status_code == 201
    assert httpx.post(url + "/v1/chat/completions", json=ask(WEAK)).status_code == 424
    (found,) = httpx.get(url + "/api/v1/candidates").json()["candidates"]
    return found  # type: ignore[no-any-return]


def run(url: str, *argv: str) -> int:
    return main(["--url", url, *argv])


def test_the_cli_lists_shows_approves_and_rejects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path, mode="replay") as url:
        found = queue_one(url)
        capsys.readouterr()

        assert run(url, "review", "list", "--json") == 0
        listed = json.loads(capsys.readouterr().out)["candidates"]
        assert [c["id"] for c in listed] == [found["id"]]

        assert run(url, "review", "list") == 0
        assert found["id"] in capsys.readouterr().out

        assert run(url, "review", "show", found["id"], "--json") == 0
        shown = json.loads(capsys.readouterr().out)
        assert shown["entry"]["responses"][0]["message"]["content"] == "answer"

        assert run(url, "review", "approve", found["id"], "--json") == 0
        assert json.loads(capsys.readouterr().out)["status"] == "approved"
        assert httpx.post(url + "/v1/chat/completions", json=ask(WEAK)).status_code == 200

        assert run(url, "review", "approve", found["id"], "--json") == 4  # already decided
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "candidate_decided"
        assert run(url, "review", "reject", "01NOPE", "--json") == 3
        capsys.readouterr()
        assert run(url, "review", "list", "--all", "--json") == 0
        assert json.loads(capsys.readouterr().out)["candidates"][0]["status"] == "approved"


def test_the_cli_rejects(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with running_server(tmp_path, mode="replay") as url:
        found = queue_one(url)
        capsys.readouterr()
        assert run(url, "review", "reject", found["id"]) == 0
        assert "rejected" in capsys.readouterr().out
        assert httpx.get(url + "/api/v1/candidates").json() == {"candidates": []}


def test_test_match_tells_a_cli_user_about_weak_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path, mode="replay") as url:
        queue_one(url)
        capsys.readouterr()
        assert run(url, "test-match", "--prompt", WEAK) == 0
        assert "weak candidate" in capsys.readouterr().out


def test_mcp_offers_the_review_queue(tmp_path: Path) -> None:
    async def call(client: Client, name: str, **args: Any) -> Any:
        result = await client.call_tool(name, args)
        return result.data if result.data is not None else json.loads(result.content[0].text)

    async def go(url: str, found: dict[str, Any]) -> None:
        async with Client(url + "/mcp/") as client:
            listed = await call(client, "list_candidates")
            assert [c["id"] for c in listed] == [found["id"]]
            detail = await call(client, "get_candidate", id=found["id"])
            assert detail["entry"]["id"] == found["entry_id"]
            done = await call(client, "approve_candidate", id=found["id"])
            assert done["status"] == "approved"
            assert await call(client, "list_candidates") == []
            decided = await call(client, "list_candidates", include_decided=True)
            assert [c["status"] for c in decided] == ["approved"]
            tools = {t.name: t for t in await client.list_tools()}
            assert "human" in (tools["approve_candidate"].description or "")

    with running_server(tmp_path, mode="replay") as url:
        asyncio.run(go(url, queue_one(url)))


def test_test_match_shows_the_tier_verdicts_to_a_cli_user(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path, mode="replay") as url:
        queue_one(url)
        capsys.readouterr()
        assert run(url, "test-match", "--prompt", WEAK) == 0
        out = capsys.readouterr().out
        assert "tiers: exact no, normalised no, alias no, words candidate" in out
        assert "meaning off" in out
