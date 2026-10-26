"""Claim the next parked call with a lease (SEM-95): one call to get everything, and two
workers never get the same call."""

import asyncio
import json
import threading
import time
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

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "move",
            "parameters": {
                "type": "object",
                "properties": {"dir": {"enum": ["n", "s"]}},
                "required": ["dir"],
            },
        },
    }
]


def req(text: str, **extra: Any) -> canonical.CanonicalRequest:
    return canonical.from_openai({"messages": [{"role": "user", "content": text}], **extra})


@pytest.fixture
def service(tmp_path: Path) -> Service:
    return Service(Database(fresh_database_url(tmp_path)), Settings(data_dir=tmp_path))


def park(service: Service, count: int, project: str = "default") -> list[str]:
    return [service.open_pending(req(f"call {n}"), project) for n in range(count)]


def test_claims_come_oldest_first_each_once_then_none(service: Service) -> None:
    ids = park(service, 3)
    got = [service.claim_next() for _ in range(3)]
    assert [c["id"] for c in got if c] == sorted(ids)
    assert service.claim_next() is None


def test_one_claim_returns_everything_needed_to_answer(service: Service) -> None:
    service.open_pending(req("what now", tools=TOOLS, max_tokens=50), "default")
    claimed = service.claim_next(worker="w1", lease_seconds=60)
    assert claimed is not None
    assert claimed["request"]["messages"][-1]["content"] == "what now"
    assert claimed["request"]["tools"][0]["function"]["name"] == "move"
    assert claimed["shape"]["tools"][0]["skeleton"] and claimed["shape"]["max_tokens"] == 50
    assert claimed["claim"]["worker"] == "w1" and claimed["claim"]["lease_seconds"] == 60
    answered = service.answer_pending(claimed["id"], claimed["shape"]["tools"][0]["skeleton"])
    assert answered  # the skeleton from the claim answers the call


def test_an_expired_lease_makes_the_call_claimable_again(service: Service) -> None:
    (only,) = park(service, 1)
    first = service.claim_next(lease_seconds=0.05)
    assert first and first["id"] == only
    assert service.claim_next() is None  # still held
    time.sleep(0.12)
    again = service.claim_next(worker="second")
    assert again and again["id"] == only and again["claim"]["worker"] == "second"


def test_an_unexpired_lease_is_not_reclaimable_by_anyone(service: Service) -> None:
    park(service, 1)
    assert service.claim_next(lease_seconds=60)
    assert service.claim_next(lease_seconds=60) is None


def test_two_workers_never_get_the_same_call(service: Service) -> None:
    ids = set(park(service, 30))
    got: list[str] = []
    guard = threading.Lock()
    start = threading.Barrier(8)

    def worker() -> None:
        start.wait()
        while (claimed := service.claim_next(lease_seconds=60)) is not None:
            with guard:
                got.append(claimed["id"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(got) == sorted(ids)  # every call once, none twice, none lost


def test_an_answered_call_is_not_claimable_and_the_list_shows_the_claim(service: Service) -> None:
    first, second = park(service, 2)
    held = service.claim_next(lease_seconds=60)
    assert held and held["id"] == first
    listed = {p["id"]: p for p in service.list_pending()}
    assert listed[first]["claimed_until"] and listed[second]["claimed_until"] is None
    service.answer_pending(first, {"content": "done"})
    service.answer_pending(second, {"content": "done too"})
    assert service.claim_next() is None


def test_claim_respects_the_project(service: Service) -> None:
    service.create_project("other")
    here = service.open_pending(req("a"), "default")
    there = service.open_pending(req("b"), "other")
    claimed = service.claim_next(project="other")
    assert claimed and claimed["id"] == there
    assert service.claim_next(project="other") is None
    other = service.claim_next(project="default")
    assert other and other["id"] == here
    with pytest.raises(SemulaError) as caught:
        service.claim_next(project="nope")
    assert caught.value.status == 404


@pytest.mark.parametrize("lease", [0, -5, 3601])
def test_a_silly_lease_is_refused(service: Service, lease: float) -> None:
    park(service, 1)
    with pytest.raises(SemulaError) as caught:
        service.claim_next(lease_seconds=lease)
    assert (caught.value.status, caught.value.param) == (422, "lease_seconds")


def test_rest_cli_and_mcp_claim(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with running_server(tmp_path, mode="record", record_timeout=0.05) as url:
        for text in ("one", "two", "three"):
            httpx.post(
                url + "/v1/chat/completions",
                json={"model": "m", "messages": [{"role": "user", "content": text}]},
            )

        rest = httpx.post(url + "/api/v1/pending/claim", json={"worker": "r", "lease_seconds": 60})
        assert rest.status_code == 200
        assert rest.json()["pending"]["request"]["messages"][-1]["content"] == "one"
        assert rest.json()["pending"]["claim"]["worker"] == "r"

        assert main(["--url", url, "--json", "claim", "--worker", "c", "--lease", "60"]) == 0
        assert json.loads(capsys.readouterr().out)["request"]["messages"][-1]["content"] == "two"

        async def via_mcp() -> Any:
            async with Client(url + "/mcp/") as client:
                result = await client.call_tool("claim_next", {"worker": "m", "lease_seconds": 60})
                return (
                    result.data if result.data is not None else json.loads(result.content[0].text)
                )

        assert asyncio.run(via_mcp())["request"]["messages"][-1]["content"] == "three"

        assert httpx.post(url + "/api/v1/pending/claim", json={}).json() == {"pending": None}
        assert main(["--url", url, "claim"]) == 0
        assert "nothing to claim" in capsys.readouterr().out
        bad = httpx.post(url + "/api/v1/pending/claim", json={"lease_seconds": 0})
        assert bad.status_code == 422
