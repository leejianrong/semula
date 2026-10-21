"""Call-log statistics: per-minute buckets, hit rate, median latency, parked (SEM-65)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

from semula.cli import main
from semula.schema import iso
from tests.integration.conftest import chat
from tests.support.server import running_server

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 21, 10, 30, 42, 500000, tzinfo=UTC)  # mid-minute, mid-second


def service_of(client: httpx.AsyncClient) -> Any:
    return client._transport.app.state.service  # type: ignore[attr-defined]


def log(client: httpx.AsyncClient, ts: datetime, outcome: str, latency: float | None = 1.0,
        project: str = "default") -> None:  # fmt: skip
    service_of(client).db.run(
        "INSERT INTO call_log (workspace_id, project_id, ts, mode, outcome, latency_ms) "
        "VALUES ('default', :p, :ts, 'replay', :o, :l)",
        {"p": project, "ts": iso(ts), "o": outcome, "l": latency},
    )


def stats(client: httpx.AsyncClient, project: str | None = None, window: str = "30m") -> Any:
    return service_of(client).log_stats(project, window, now=NOW)


def at(minute: int, second: float = 0.0) -> datetime:
    return NOW.replace(minute=minute, second=0, microsecond=0) + timedelta(seconds=second)


async def test_an_empty_window_is_all_zero_buckets_and_no_rates(replay: httpx.AsyncClient) -> None:
    out = stats(replay)
    assert len(out["buckets"]) == 30
    assert all(
        b["hits"] == b["misses"] == b["recorded"] == b["timed_out"] == 0 for b in out["buckets"]
    )
    assert out["hit_rate"] is None and out["median_latency_ms"] is None and out["parked"] == 0
    assert out["totals"] == {"calls": 0, "hits": 0, "misses": 0, "recorded": 0, "timed_out": 0}


async def test_buckets_are_contiguous_minutes_ending_with_the_current_one(
    replay: httpx.AsyncClient,
) -> None:
    out = stats(replay, window="5m")
    assert [b["ts"] for b in out["buckets"]] == [
        "2026-10-21T10:26:00.000Z",
        "2026-10-21T10:27:00.000Z",
        "2026-10-21T10:28:00.000Z",
        "2026-10-21T10:29:00.000Z",
        "2026-10-21T10:30:00.000Z",
    ]
    assert out["since"] == "2026-10-21T10:26:00.000Z" and out["until"] == iso(NOW)
    assert out["window"] == "5m"


async def test_each_outcome_lands_in_its_own_minute(replay: httpx.AsyncClient) -> None:
    log(replay, at(30, 1), "hit")
    log(replay, at(30, 2), "hit")
    log(replay, at(30, 3), "miss")
    log(replay, at(29, 10), "recorded")
    log(replay, at(29, 20), "record_timeout")
    log(replay, at(29, 30), "record_timeout")
    log(replay, at(10, 0), "hit")
    out = stats(replay)
    by_minute = {b["ts"][11:16]: b for b in out["buckets"]}
    assert (by_minute["10:30"]["hits"], by_minute["10:30"]["misses"]) == (2, 1)
    assert (by_minute["10:29"]["recorded"], by_minute["10:29"]["timed_out"]) == (1, 2)
    assert by_minute["10:10"]["hits"] == 1
    assert out["totals"] == {"calls": 7, "hits": 3, "misses": 1, "recorded": 1, "timed_out": 2}
    assert out["hit_rate"] == pytest.approx(3 / 7, abs=1e-3)


async def test_bucket_edges(replay: httpx.AsyncClient) -> None:
    first = at(1)  # the first slot of a 30m window ending in minute 30 starts at 10:01:00.000
    log(replay, first - timedelta(milliseconds=1), "hit")  # one tick before the window
    log(replay, first, "miss")  # exactly the first instant of the first slot
    log(replay, at(1, 59.999), "miss")  # the last instant of that slot
    log(replay, at(2), "hit")  # the very next slot
    log(replay, NOW + timedelta(seconds=10), "hit")  # a clock skew a little ahead still counts
    out = stats(replay)
    assert out["buckets"][0]["ts"] == "2026-10-21T10:01:00.000Z"
    assert out["buckets"][0]["misses"] == 2 and out["buckets"][0]["hits"] == 0
    assert out["buckets"][1]["hits"] == 1
    assert out["buckets"][-1]["hits"] == 1
    assert out["totals"]["calls"] == 4  # the row before the window is not counted


async def test_a_window_that_crosses_midnight_and_an_hour(replay: httpx.AsyncClient) -> None:
    out = service_of(replay).log_stats(None, "90m", now=datetime(2026, 1, 1, 0, 10, tzinfo=UTC))
    assert out["buckets"][0]["ts"] == "2025-12-31T22:41:00.000Z"
    assert len(out["buckets"]) == 90 and out["buckets"][-1]["ts"] == "2026-01-01T00:10:00.000Z"


async def test_the_project_filter(replay: httpx.AsyncClient) -> None:
    await replay.post("/api/v1/projects", json={"id": "other"})
    log(replay, at(30), "hit")
    log(replay, at(30), "miss", project="other")
    log(replay, at(30), "miss", project="other")
    assert stats(replay)["totals"]["calls"] == 3
    assert stats(replay, "other")["totals"] == {
        "calls": 2, "hits": 0, "misses": 2, "recorded": 0, "timed_out": 0,
    }  # fmt: skip
    assert stats(replay, "default")["totals"]["hits"] == 1
    assert (await replay.get("/api/v1/log/stats", params={"project": "nope"})).status_code == 404


async def test_the_median_covers_hits_and_misses_only(replay: httpx.AsyncClient) -> None:
    for latency in (2.0, 4.0, 100.0):
        log(replay, at(30), "hit", latency)
    log(replay, at(30), "miss", 10.0)
    log(replay, at(30), "recorded", 90000.0)  # waited on an agent: not a replay latency
    log(replay, at(30), "record_timeout", 120000.0)
    log(replay, at(30), "hit", None)  # an old row without a latency
    assert stats(replay)["median_latency_ms"] == pytest.approx(7.0)  # median of 2, 4, 10, 100


async def test_hit_rate_is_hits_over_every_call(replay: httpx.AsyncClient) -> None:
    log(replay, at(30), "hit")
    assert stats(replay)["hit_rate"] == 1.0
    log(replay, at(30), "miss")
    log(replay, at(30), "miss")
    log(replay, at(30), "hit")
    assert stats(replay)["hit_rate"] == 0.5


async def test_parked_counts_open_requests_whatever_the_window(
    record: httpx.AsyncClient,
) -> None:
    await record.post("/api/v1/projects", json={"id": "other"})
    calls = [
        asyncio.create_task(record.post("/v1/chat/completions", json=chat("one"))),
        asyncio.create_task(record.post("/v1/chat/completions", json=chat("two"))),
        asyncio.create_task(record.post("/v1/projects/other/chat/completions", json=chat("three"))),
    ]
    for _ in range(300):
        if len((await record.get("/api/v1/pending")).json()["pending"]) == 3:
            break
        await asyncio.sleep(0.01)
    assert stats(record)["parked"] == 3
    assert stats(record, "other")["parked"] == 1
    assert service_of(record).log_stats(None, "1m", now=NOW - timedelta(days=3))["parked"] == 3
    pending = (await record.get("/api/v1/pending", params={"project": "other"})).json()["pending"]
    await record.post(f"/api/v1/pending/{pending[0]['id']}/answer", json={"content": "x"})
    assert stats(record)["parked"] == 2
    for call in calls:
        call.cancel()


@pytest.mark.parametrize(("window", "buckets"), [("1m", 1), ("45m", 45), ("2h", 120), ("1d", 1440)])
async def test_windows(replay: httpx.AsyncClient, window: str, buckets: int) -> None:
    assert len(stats(replay, window=window)["buckets"]) == buckets


@pytest.mark.parametrize("window", ["0m", "-5m", "25h", "2d", "abc", "", "30", "1.5h", "m"])
async def test_a_bad_window_is_a_422(replay: httpx.AsyncClient, window: str) -> None:
    resp = await replay.get("/api/v1/log/stats", params={"window": window})
    assert resp.status_code == 422 and resp.json()["error"]["param"] == "window"


async def test_the_endpoint_reflects_real_calls(replay: httpx.AsyncClient) -> None:
    await replay.post(
        "/api/v1/projects/default/entries",
        json={"request": chat("hi"), "response": {"content": "yo"}},
    )
    await replay.post("/v1/chat/completions", json=chat("hi"))
    await replay.post("/v1/chat/completions", json=chat("hi"))
    await replay.post("/v1/chat/completions", json=chat("unknown"))
    out = (await replay.get("/api/v1/log/stats")).json()
    assert out["totals"]["hits"] == 2 and out["totals"]["misses"] == 1
    assert out["hit_rate"] == pytest.approx(2 / 3, abs=1e-3) and out["median_latency_ms"] > 0
    assert len(out["buckets"]) == 30 and out["window"] == "30m"


def test_the_cli_and_mcp_report_the_same_numbers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def mcp(url: str) -> Any:
        async with Client(url + "/mcp/") as client:
            result = await client.call_tool("log_stats", {"window": "10m"})
            return result.data if result.data is not None else json.loads(result.content[0].text)

    with running_server(tmp_path, mode="replay") as url:
        httpx.post(url + "/api/v1/projects/default/entries",
                   json={"request": chat("hi"), "response": {"content": "yo"}})  # fmt: skip
        httpx.post(url + "/v1/chat/completions", json=chat("hi"))
        httpx.post(url + "/v1/chat/completions", json=chat("nope"))
        capsys.readouterr()
        assert main(["--url", url, "log", "--stats", "--window", "10m"]) == 0
        human = capsys.readouterr().out
        assert "10m" in human and "2 calls" in human and "hit rate 50%" in human
        assert "parked 0" in human
        assert main(["--url", url, "log", "--stats", "--json"]) == 0
        body = json.loads(capsys.readouterr().out)
        assert body["totals"]["calls"] == 2 and len(body["buckets"]) == 30
        found = asyncio.run(mcp(url))
        assert len(found["buckets"]) == 10 and found["totals"]["hits"] == 1
        assert main(["--url", url, "log", "--stats", "--window", "9z"]) != 0
