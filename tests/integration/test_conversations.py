"""Group parked and logged calls by conversation (SEM-98)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

from semula import canonical
from semula.cli import main
from semula.config import Settings
from semula.db import Database
from semula.errors import SemulaError
from semula.service import Service
from tests.support.db import fresh_database_url
from tests.support.server import running_server

pytestmark = pytest.mark.integration


def turns(*texts: str) -> canonical.CanonicalRequest:
    """A chat history: user, assistant, user, ... as an agent loop resends it."""
    roles = ("user", "assistant")
    body = {"messages": [{"role": roles[i % 2], "content": t} for i, t in enumerate(texts)]}
    return canonical.from_openai(body)


@pytest.fixture
def service(tmp_path: Path) -> Service:
    return Service(Database(fresh_database_url(tmp_path)), Settings(data_dir=tmp_path))


def play(service: Service, project: str = "default") -> list[str]:
    """Three turns of one game, each answered before the next, and an unrelated call."""
    ids = []
    history = ["start"]
    for n in range(3):
        pid = service.open_pending(turns(*history), project)
        ids.append(pid)
        service.answer_pending(pid, {"content": f"reply {n}"})
        history += [f"reply {n}", f"move {n}"]
    ids.append(service.open_pending(turns("something else"), project))
    return ids


def test_calls_sharing_a_prefix_are_grouped_and_the_added_messages_shown(
    service: Service,
) -> None:
    first, second, third, other = play(service)
    groups = service.list_conversations()
    by_root = {g["id"]: g for g in groups}
    game = by_root[first]
    assert [c["id"] for c in game["calls"]] == [first, second, third]
    assert [c["depth"] for c in game["calls"]] == [1, 2, 3]
    assert [m["content"] for m in game["calls"][1]["added"]] == ["reply 0", "move 0"]
    assert [m["content"] for m in game["calls"][2]["added"]] == ["reply 1", "move 1"]
    assert by_root[other]["size"] == 1  # unrelated calls stay apart
    assert service.list_conversations(min_size=2)[0]["id"] == first
    assert len(service.list_conversations(min_size=2)) == 1


def test_the_pending_views_say_where_a_call_sits_and_what_it_added(service: Service) -> None:
    first, second, third, other = play(service)
    waiting = {p["id"]: p for p in service.list_pending()}  # only the unanswered one waits
    assert set(waiting) == {other}
    assert waiting[other]["conversation"]["size"] == 1

    shown = service.get_pending(third)["conversation"]
    assert shown["id"] == first and shown["parent"] == second
    assert (shown["depth"], shown["size"]) == (3, 3)
    assert [m["content"] for m in shown["added"]] == ["reply 1", "move 1"]
    assert service.get_pending(first)["conversation"]["added"] == []  # nothing before it

    answered = {p["id"]: p for p in service.list_pending(None)}
    assert answered[second]["conversation"]["id"] == first


def test_groups_are_per_project(service: Service) -> None:
    service.create_project("other")
    a = service.open_pending(turns("hello"), "default")
    b = service.open_pending(turns("hello", "hi", "again"), "other")
    roots = {g["project"]: g["id"] for g in service.list_conversations()}
    assert roots == {"default": a, "other": b}  # same words, different project: apart


def test_logged_calls_are_grouped_by_the_request_they_point_at(service: Service) -> None:
    first, second, third, _ = play(service)
    for pid in (first, second, third):
        service.log_call("recorded", 0.0, pending_id=pid, project="default", req=turns("x"))
    service.log_call("miss", 0.0, project="default", req=turns("start"))  # unplaceable
    (group,) = service.list_conversations(source="log", min_size=2)
    assert [c["id"] for c in group["calls"]] == [f"p:{i}" for i in (first, second, third)]
    assert [m["content"] for m in group["calls"][1]["added"]] == ["reply 0", "move 0"]
    assert group["calls"][0]["calls"] == 1


def test_a_bad_source_is_refused(service: Service) -> None:
    with pytest.raises(SemulaError) as caught:
        service.list_conversations(source="nope")
    assert (caught.value.status, caught.value.param) == (422, "source")


def test_rest_cli_and_mcp_list_conversations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path, mode="record", record_timeout=0.05) as url:
        history: list[dict[str, str]] = [{"role": "user", "content": "start"}]
        for n in range(3):
            httpx.post(url + "/v1/chat/completions", json={"model": "m", "messages": history})
            history += [
                {"role": "assistant", "content": f"reply {n}"},
                {"role": "user", "content": f"move {n}"},
            ]

        rest = httpx.get(url + "/api/v1/conversations", params={"min_size": 2}).json()
        (group,) = rest["conversations"]
        assert group["size"] == 3
        assert [m["content"] for m in group["calls"][2]["added"]] == ["reply 1", "move 1"]

        assert main(["--url", url, "--json", "conversations"]) == 0
        assert json.loads(capsys.readouterr().out)[0]["size"] == 3
        assert main(["--url", url, "conversations"]) == 0
        text = capsys.readouterr().out
        assert "3 calls" in text and "move 1" in text

        async def via_mcp() -> Any:
            async with Client(url + "/mcp/") as client:
                result = await client.call_tool("list_conversations", {})
                return (
                    result.data if result.data is not None else json.loads(result.content[0].text)
                )

        assert asyncio.run(via_mcp())[0]["size"] == 3
        assert httpx.get(url + "/api/v1/conversations", params={"source": "x"}).status_code == 422
