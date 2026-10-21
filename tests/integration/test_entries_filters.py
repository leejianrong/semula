"""Entries list: filters, sort and per-row flags (SEM-64)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

from semula.cli import main
from tests.integration.conftest import chat
from tests.integration.test_entries import LOG_TOOL
from tests.integration.test_lexical_match import BASE, WEAK, ask
from tests.support.server import running_server

pytestmark = pytest.mark.integration

URL = "/api/v1/projects/default/entries"


async def author(client: httpx.AsyncClient, prompt: str, response: dict, **extra: object) -> str:
    resp = await client.post(URL, json={"request": {**chat(prompt), **extra}, "response": response})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


TOOL_ANSWER = {
    "message": {
        "content": None,
        "tool_calls": [{"function": {"name": "log", "arguments": '{"id":"1"}'}}],
    }
}


@pytest.fixture
async def project(replay: httpx.AsyncClient) -> dict[str, str]:
    """alpha: plain, 2 hits. bravo: pinned, 0 hits. charlie: tool call, 1 hit.
    base: has one approved alias, 0 hits."""
    ids = {
        "alpha": await author(replay, "alpha", {"content": "a"}),
        "bravo": await author(replay, "bravo", {"content": "b"}),
        "charlie": await author(replay, "charlie", TOOL_ANSWER, tools=[LOG_TOOL]),
    }
    ids["base"] = str(
        (
            await replay.post(URL, json={"request": ask(BASE), "response": {"content": "answer"}})
        ).json()["id"]
    )
    await replay.put(f"/api/v1/entries/{ids['bravo']}", json={"version": 1, "pinned": True})
    for _ in range(2):
        await replay.post("/v1/chat/completions", json=chat("alpha"))
    await replay.post("/v1/chat/completions", json=chat("charlie", tools=[LOG_TOOL]))
    await replay.post("/v1/chat/completions", json=ask(WEAK))  # a miss: queues a candidate
    (cand,) = (await replay.get("/api/v1/candidates")).json()["candidates"]
    assert (await replay.post(f"/api/v1/candidates/{cand['id']}/approve")).status_code == 200
    return ids


async def listing(client: httpx.AsyncClient, **params: object) -> dict:
    resp = await client.get(URL, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


def names(found: dict, ids: dict[str, str]) -> list[str]:
    back = {v: k for k, v in ids.items()}
    return [back[e["id"]] for e in found["entries"]]


async def test_every_row_carries_an_alias_count_and_a_tool_call_flag(
    replay: httpx.AsyncClient, project: dict[str, str]
) -> None:
    rows = {e["id"]: e for e in (await listing(replay))["entries"]}
    assert rows[project["base"]]["alias_count"] == 1
    assert rows[project["alpha"]]["alias_count"] == 0
    assert rows[project["charlie"]]["has_tools"] is True
    assert rows[project["alpha"]]["has_tools"] is False
    assert "request" not in rows[project["alpha"]]  # still the lean projection


async def test_without_parameters_the_list_is_unchanged(
    replay: httpx.AsyncClient, project: dict[str, str]
) -> None:
    found = await listing(replay)
    assert found["total"] == 4
    assert names(found, project) == ["alpha", "bravo", "charlie", "base"]  # creation order


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"pinned": True}, ["bravo"]),
        ({"pinned": False}, ["alpha", "charlie", "base"]),
        ({"has_alias": True}, ["base"]),
        ({"has_alias": False}, ["alpha", "bravo", "charlie"]),
        ({"tool_calls": True}, ["charlie"]),
        ({"tool_calls": False}, ["alpha", "bravo", "base"]),
        ({"never_hit": True}, ["bravo", "base"]),
        ({"never_hit": False}, ["alpha", "charlie"]),
        ({"never_hit": True, "pinned": False}, ["base"]),
        ({"never_hit": True, "has_alias": True}, ["base"]),
        ({"tool_calls": True, "never_hit": True}, []),
        ({"tool_calls": True, "pinned": True}, []),
        ({"never_hit": True, "q": "bravo"}, ["bravo"]),
    ],
)
async def test_filters_and_their_combinations(
    replay: httpx.AsyncClient,
    project: dict[str, str],
    params: dict[str, object],
    expected: list[str],
) -> None:
    found = await listing(replay, **{k: str(v).lower() for k, v in params.items()})
    assert names(found, project) == expected
    assert found["total"] == len(expected)


async def test_total_counts_the_filtered_set_not_the_page(
    replay: httpx.AsyncClient, project: dict[str, str]
) -> None:
    found = await listing(replay, never_hit="false", limit=1)
    assert found["total"] == 2 and len(found["entries"]) == 1
    second = await listing(replay, never_hit="false", limit=1, offset=1)
    assert second["total"] == 2 and second["entries"][0]["id"] != found["entries"][0]["id"]
    assert (await listing(replay, never_hit="false", offset=5))["entries"] == []


async def test_sort_by_hits_updated_and_created_with_an_order(
    replay: httpx.AsyncClient, project: dict[str, str]
) -> None:
    hits = await listing(replay, sort="hits")
    assert [e["hit_count"] for e in hits["entries"]] == [2, 1, 0, 0]  # most first by default
    assert names(await listing(replay, sort="hits", order="asc"), project)[-1] == "alpha"
    # the pin edit touched bravo last
    assert names(await listing(replay, sort="updated"), project)[0] == "bravo"
    assert names(await listing(replay, sort="updated", order="asc"), project)[-1] == "bravo"
    assert names(await listing(replay, sort="created"), project)[0] == "base"
    assert names(await listing(replay, sort="created", order="asc"), project)[0] == "alpha"
    assert names(await listing(replay, order="desc"), project)[0] == "base"  # id order, reversed


async def test_sorted_pages_do_not_repeat_or_skip_rows(replay: httpx.AsyncClient) -> None:
    for n in range(7):
        await author(replay, f"p{n}", {"content": "x"})  # all tied on hits
    seen: list[str] = []
    for offset in range(0, 7, 3):
        page = await listing(replay, sort="hits", limit=3, offset=offset)
        seen += [e["id"] for e in page["entries"]]
    assert len(seen) == 7 and len(set(seen)) == 7


async def test_bad_sort_or_order_or_flag_is_a_422(replay: httpx.AsyncClient) -> None:
    assert (await replay.get(URL, params={"sort": "magic"})).status_code == 422
    assert (await replay.get(URL, params={"order": "sideways"})).status_code == 422
    assert (await replay.get(URL, params={"pinned": "maybe"})).status_code == 422


async def test_the_tool_call_filter_agrees_with_the_flag_however_the_entry_was_written(
    record: httpx.AsyncClient,
) -> None:
    """Authored, answered from the queue and edited entries all report the same way."""
    call = asyncio.create_task(
        record.post("/v1/chat/completions", json=chat("queued", tools=[LOG_TOOL]))
    )
    for _ in range(200):
        pending = (await record.get("/api/v1/pending")).json()["pending"]
        if pending:
            break
        await asyncio.sleep(0.01)
    await record.post(f"/api/v1/pending/{pending[0]['id']}/answer", json=TOOL_ANSWER)
    await call
    await author(record, "authored", TOOL_ANSWER, tools=[LOG_TOOL])
    plain = await author(record, "text then tool", {"content": "hi"}, tools=[LOG_TOOL])
    edited = await record.put(
        f"/api/v1/entries/{plain}", json={"version": 1, "response": TOOL_ANSWER}
    )
    assert edited.status_code == 200, edited.text
    await author(record, "mentions tool_calls in text", {"content": 'a "tool_calls": [ in prose'})
    everything = (await listing(record))["entries"]
    flagged = {e["id"] for e in everything if e["has_tools"]}
    filtered = {e["id"] for e in (await listing(record, tool_calls="true"))["entries"]}
    assert flagged == filtered and len(flagged) == 3


def test_the_cli_and_mcp_take_the_same_filters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def mcp(url: str) -> Any:
        async with Client(url + "/mcp/") as client:
            result = await client.call_tool(
                "list_entries", {"project": "default", "tool_calls": True, "sort": "hits"}
            )
            return result.data if result.data is not None else json.loads(result.content[0].text)

    with running_server(tmp_path, mode="replay") as url:
        for prompt, answer, extra in (
            ("plain", {"content": "x"}, {}),
            ("calls a tool", TOOL_ANSWER, {"tools": [LOG_TOOL]}),
        ):
            body = {"request": {**chat(prompt), **extra}, "response": answer}
            assert httpx.post(url + URL, json=body).status_code == 201
        capsys.readouterr()
        assert main(["--url", url, "entry", "list", "default", "--tool-calls", "--json"]) == 0
        listed = json.loads(capsys.readouterr().out)
        assert [e["last_message"] for e in listed["entries"]] == ["calls a tool"]
        assert listed["entries"][0]["has_tools"] is True
        assert main(["--url", url, "entry", "list", "default", "--no-tool-calls"]) == 0
        human = capsys.readouterr().out
        assert "plain" in human and "calls a tool" not in human and "(1 of 1)" in human
        assert (
            main(["--url", url, "entry", "list", "default", "--never-hit", "--sort", "hits"]) == 0
        )
        assert "(2 of 2)" in capsys.readouterr().out
        found = asyncio.run(mcp(url))
        assert found["total"] == 1 and found["entries"][0]["alias_count"] == 0
