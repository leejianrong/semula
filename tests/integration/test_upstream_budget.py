"""The budget and the dry run for record-through (SEM-93, ADR-0013)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from semula.app import create_app
from semula.config import Settings
from semula.scope import Scope
from tests.integration.conftest import chat
from tests.integration.test_record_through import build, through
from tests.support.db import fresh_database_url
from tests.support.upstream import KEY, FakeUpstream

pytestmark = pytest.mark.integration  # each fake answer uses 7 prompt + 3 completion tokens


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
async def client(tmp_path: Path, upstream: FakeUpstream) -> AsyncIterator[httpx.AsyncClient]:
    async with build(tmp_path, upstream) as c:
        yield c


async def usage(client: httpx.AsyncClient) -> dict[str, Any]:
    resp = await client.get("/api/v1/projects/default/upstream")
    assert resp.status_code == 200
    return dict(resp.json())


async def test_the_usage_counts_calls_and_the_tokens_the_answers_reported(
    client: httpx.AsyncClient,
) -> None:
    await through(client)
    await client.post("/v1/chat/completions", json=chat("one"))
    await client.post("/v1/chat/completions", json=chat("two", stream=True))
    got = await usage(client)
    assert (got["calls"], got["prompt_tokens"], got["completion_tokens"]) == (2, 14, 6)
    assert got["max_calls"] == 100 and got["max_tokens"] is None and got["dry_run"] is False


async def test_the_token_cap_stops_the_next_call_once_it_is_spent(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    await through(client, upstream_max_tokens=15)
    assert (await client.post("/v1/chat/completions", json=chat("a"))).status_code == 200  # 10
    assert (await client.post("/v1/chat/completions", json=chat("b"))).status_code == 200  # 20
    over = await client.post("/v1/chat/completions", json=chat("c"))
    assert over.status_code == 429
    assert over.json()["error"]["code"] == "semula_upstream_budget"
    assert "upstream_max_tokens" in over.json()["error"]["message"]
    assert upstream.calls == 2
    assert (await client.post("/v1/chat/completions", json=chat("a"))).status_code == 200  # replay


async def test_the_call_cap_error_names_the_cap_and_says_how_to_go_on(
    client: httpx.AsyncClient,
) -> None:
    await through(client, upstream_max_calls=1)
    await client.post("/v1/chat/completions", json=chat("a"))
    over = await client.post("/v1/chat/completions", json=chat("b"))
    assert over.status_code == 429 and "upstream_max_calls" in over.text
    assert over.json()["upstream"]["calls"] == 1 and "--reset" in over.text


async def test_a_dry_run_lists_what_would_be_called_and_calls_nothing(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    await through(client, upstream_dry_run=True)
    for prompt in ("first prompt", "second prompt", "first prompt"):
        resp = await client.post("/v1/chat/completions", json=chat(prompt))
        assert resp.status_code == 424
        assert resp.json()["error"]["code"] == "semula_upstream_dry_run"
    assert upstream.calls == 0
    got = await usage(client)
    assert got["calls"] == 0 and got["dry_run"] is True
    assert {(m["prompt"], m["count"]) for m in got["would_forward"]} == {
        ("first prompt", 2),
        ("second prompt", 1),
    }
    assert (await client.get("/api/v1/projects/default/entries")).json()["entries"] == []
    outcomes = {c["outcome"] for c in (await client.get("/api/v1/log")).json()["calls"]}
    assert outcomes == {"dry_run"}


async def test_a_dry_run_still_replays_what_is_recorded(client: httpx.AsyncClient) -> None:
    await through(client)
    await client.post("/v1/chat/completions", json=chat("known"))
    await through(client, upstream_dry_run=True)
    assert (await client.post("/v1/chat/completions", json=chat("known"))).status_code == 200
    assert (await client.post("/v1/chat/completions", json=chat("new"))).status_code == 424


async def test_turning_the_dry_run_off_forwards_again(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    await through(client, upstream_dry_run=True)
    await client.post("/v1/chat/completions", json=chat("later"))
    await client.patch("/api/v1/projects/default", json={"settings": {"upstream_dry_run": None}})
    assert (await client.post("/v1/chat/completions", json=chat("later"))).status_code == 200
    assert upstream.calls == 1


async def test_resetting_starts_the_count_again(client: httpx.AsyncClient) -> None:
    await through(client, upstream_max_calls=1)
    await client.post("/v1/chat/completions", json=chat("a"))
    assert (await client.post("/v1/chat/completions", json=chat("b"))).status_code == 429
    reset = await client.post("/api/v1/projects/default/upstream/reset")
    assert reset.status_code == 200 and reset.json()["calls"] == 0
    assert reset.json()["prompt_tokens"] == 0
    assert (await client.post("/v1/chat/completions", json=chat("b"))).status_code == 200


async def test_the_stats_carry_the_forwarded_calls_and_the_upstream_total(
    client: httpx.AsyncClient,
) -> None:
    await through(client)
    await client.post("/v1/chat/completions", json=chat("counted"))
    stats = (await client.get("/api/v1/log/stats")).json()
    assert stats["totals"]["forwarded"] == 1 and stats["totals"]["calls"] == 1
    assert stats["upstream"] == {"calls": 1, "prompt_tokens": 7, "completion_tokens": 3}
    only = (await client.get("/api/v1/log/stats", params={"project": "default"})).json()
    assert only["upstream"]["calls"] == 1


async def test_a_project_bound_or_record_token_reads_but_never_resets_or_sets(
    tmp_path: Path,
) -> None:
    upstream = FakeUpstream()
    settings = Settings(
        data_dir=tmp_path,
        database_url=fresh_database_url(tmp_path),
        hosted=True,
        mode="record",
        upstream_key=KEY,
        upstream_tenants=True,
    )
    app = create_app(settings, upstream_transport=upstream.transport)
    service = app.state.service
    service.create_workspace("w")
    mine = service.for_scope(Scope("w"))
    mine.create_project("shop")
    mine.create_project("other")
    record = mine.create_token("rec", "record", project="shop")["token"]
    admin = mine.create_token("adm", "admin", project="shop")["token"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:

        def h(secret: str) -> dict[str, str]:
            return {"Authorization": f"Bearer {secret}"}

        assert (await c.get("/api/v1/projects/shop/upstream", headers=h(record))).status_code == 200
        reset = await c.post("/api/v1/projects/shop/upstream/reset", headers=h(record))
        assert reset.status_code == 403
        # a token bound to shop reaches nothing of other's, read or reset, even an admin one
        for path, method in (("upstream", "GET"), ("upstream/reset", "POST")):
            resp = await c.request(method, f"/api/v1/projects/other/{path}", headers=h(admin))
            assert resp.status_code in (403, 404)
        assert (
            await c.post("/api/v1/projects/shop/upstream/reset", headers=h(admin))
        ).status_code == 200
        turn_on = await c.patch(
            "/api/v1/projects/shop",
            json={"settings": {"record_through": True}},
            headers=h(record),
        )
        assert turn_on.status_code == 403
