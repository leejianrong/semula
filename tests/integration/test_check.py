"""A miss report for a run (SEM-104): every miss, its nearest entry, a non-zero exit."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from semula.cli import main
from semula.schema import iso
from tests.integration.conftest import chat
from tests.support.server import running_server

pytestmark = pytest.mark.integration


async def seed(client: httpx.AsyncClient, prompt: str) -> str:
    resp = await client.post(
        "/api/v1/projects/default/entries",
        json={
            "request": {"messages": [{"role": "user", "content": prompt}]},
            "response": {"content": "ok"},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def test_a_clean_run_passes(replay: httpx.AsyncClient) -> None:
    await seed(replay, "what is the capital of France")
    assert (
        await replay.post("/v1/chat/completions", json=chat("what is the capital of France"))
    ).status_code == 200
    report = (await replay.get("/api/v1/check")).json()
    assert report["ok"] is True
    assert report["hits"] == 1 and report["misses"] == 0 and report["missed"] == []


async def test_each_miss_is_listed_with_its_nearest_entry(replay: httpx.AsyncClient) -> None:
    entry = await seed(replay, "what is the capital of France")
    resp = await replay.post("/v1/chat/completions", json=chat("what is the capital of Spain"))
    assert resp.status_code == 424
    report = (await replay.get("/api/v1/check")).json()
    assert report["ok"] is False and report["misses"] == 1
    miss = report["missed"][0]
    assert miss["outcome"] == "miss"
    assert "Spain" in miss["preview"]
    assert miss["nearest"]["entry_id"] == entry
    assert 0 <= miss["nearest"]["score"] <= 1


async def test_a_miss_with_no_entries_has_no_nearest(replay: httpx.AsyncClient) -> None:
    await replay.post("/v1/chat/completions", json=chat("anything"))
    miss = (await replay.get("/api/v1/check")).json()["missed"][0]
    assert miss["nearest"] is None


async def test_since_leaves_out_earlier_calls(replay: httpx.AsyncClient) -> None:
    await replay.post("/v1/chat/completions", json=chat("early"))
    await asyncio.sleep(0.01)
    cutoff = iso(datetime.now(UTC))
    await asyncio.sleep(0.01)
    await replay.post("/v1/chat/completions", json=chat("late"))
    everything = (await replay.get("/api/v1/check")).json()
    later = (await replay.get("/api/v1/check", params={"since": cutoff})).json()
    assert everything["misses"] == 2
    assert later["misses"] == 1 and "late" in later["missed"][0]["preview"]


async def test_an_unknown_project_is_a_404(replay: httpx.AsyncClient) -> None:
    resp = await replay.get("/api/v1/check", params={"project": "nope"})
    assert resp.status_code == 404


def test_the_cli_exits_non_zero_on_a_miss(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path) as url:
        assert main(["--url", url, "check"]) == 0
        capsys.readouterr()
        httpx.post(url + "/v1/chat/completions", json=chat("never recorded"))
        assert main(["--url", url, "check"]) == 1
        text = capsys.readouterr().out
        assert "never recorded" in text and "1 miss" in text
        assert main(["--url", url, "check", "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["ok"] is False
