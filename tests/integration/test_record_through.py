"""Record-through: a miss goes to a real provider once, and is replayed free after (SEM-92,
ADR-0013). The provider is a fake; CI never spends a credit."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from semula.app import create_app
from semula.config import Settings
from tests.integration.conftest import chat
from tests.support.db import fresh_database_url
from tests.support.upstream import KEY, FakeUpstream

pytestmark = pytest.mark.integration


def build(tmp_path: Path, upstream: FakeUpstream, **overrides: Any) -> httpx.AsyncClient:
    overrides.setdefault("mode", "record")
    overrides.setdefault("upstream_key", KEY)
    overrides.setdefault("record_timeout", 0.2)
    settings = Settings(data_dir=tmp_path, database_url=fresh_database_url(tmp_path), **overrides)
    app = create_app(settings, upstream_transport=upstream.transport)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def through(client: httpx.AsyncClient, **settings: Any) -> None:
    resp = await client.patch(
        "/api/v1/projects/default", json={"settings": {"record_through": True, **settings}}
    )
    assert resp.status_code == 200, resp.text


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
async def client(tmp_path: Path, upstream: FakeUpstream) -> AsyncIterator[httpx.AsyncClient]:
    async with build(tmp_path, upstream) as c:
        await through(c)
        yield c


def text(resp: httpx.Response) -> str:
    return str(resp.json()["choices"][0]["message"]["content"])


async def test_a_miss_calls_the_upstream_once_and_later_calls_replay_free(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    first = await client.post("/v1/chat/completions", json=chat("who goes first"))
    assert first.status_code == 200 and text(first) == "upstream says: who goes first"
    assert upstream.calls == 1
    for _ in range(3):
        again = await client.post("/v1/chat/completions", json=chat("who goes first"))
        assert text(again) == "upstream says: who goes first"
    assert upstream.calls == 1
    entries = (await client.get("/api/v1/projects/default/entries")).json()["entries"]
    assert len(entries) == 1
    outcomes = [c["outcome"] for c in (await client.get("/api/v1/log")).json()["calls"]]
    assert sorted(outcomes) == ["forwarded", "hit", "hit", "hit"]


async def test_identical_misses_at_once_make_one_upstream_call(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    results = await asyncio.gather(
        *(client.post("/v1/chat/completions", json=chat("same")) for _ in range(5))
    )
    assert {text(r) for r in results} == {"upstream says: same"}
    assert upstream.calls == 1


async def test_a_streamed_answer_is_saved_with_its_chunks_and_usage(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    body = chat("stream me", stream=True, stream_options={"include_usage": True})
    first = await client.post("/v1/chat/completions", json=body)
    replay = await client.post("/v1/chat/completions", json=body)
    assert upstream.calls == 1
    assert upstream.bodies[0]["stream_options"] == {"include_usage": True}
    assert replay.text.count("data: ") == first.text.count("data: ")
    assert "upstream says: stream me" in "".join(
        json.loads(line[6:])["choices"][0]["delta"].get("content", "")
        for line in replay.text.splitlines()
        if line.startswith("data: {") and json.loads(line[6:])["choices"]
    )
    assert '"total_tokens":10' in replay.text
    usage = (await client.get("/api/v1/entries/" + await entry_id(client))).json()
    assert usage["responses"][0]["usage"]["completion_tokens"] == 3


async def entry_id(client: httpx.AsyncClient) -> str:
    return str((await client.get("/api/v1/projects/default/entries")).json()["entries"][0]["id"])


async def test_the_app_still_gets_usage_when_it_asked_for_a_stream_without_it(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    await client.post("/v1/chat/completions", json=chat("no usage asked", stream=True))
    assert upstream.bodies[0]["stream_options"]["include_usage"] is True  # we need it to record


async def test_the_key_goes_upstream_alone_and_the_apps_own_headers_do_not(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    await client.post(
        "/v1/chat/completions",
        json=chat("headers"),
        headers={"Authorization": "Bearer the-apps-own-secret", "X-Custom": "mine"},
    )
    sent = upstream.requests[0]
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert "x-custom" not in sent.headers
    assert "the-apps-own-secret" not in sent.headers["authorization"]


async def test_the_key_is_in_no_entry_log_export_setting_or_listing(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    await client.post("/v1/chat/completions", json=chat("leak check"))
    seen = [
        (await client.get(url)).text
        for url in (
            "/api/v1/projects/default",
            "/api/v1/projects",
            "/api/v1/projects/default/entries",
            f"/api/v1/entries/{await entry_id(client)}",
            f"/api/v1/entries/{await entry_id(client)}/history",
            "/api/v1/log",
            "/api/v1/log/stats",
            "/api/v1/projects/default/export",
            "/api/v1/pending",
            "/api/v1/usage",
            "/api/v1/mode",
            "/health",
            "/v1/models",
        )
    ]
    assert not [t for t in seen if KEY in t]
    assert KEY not in repr(Settings(data_dir=tmp_path, upstream_key=KEY))
    for path in tmp_path.rglob("*"):  # nor in anything written to disk
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path


async def test_the_key_is_not_echoed_in_an_error_even_if_the_provider_echoes_it(
    tmp_path: Path,
) -> None:
    plain = "upstream-secret-without-a-known-shape"  # only an exact match can scrub this

    def reject(body: dict[str, Any]) -> httpx.Response:
        message = f"Incorrect API key provided: {plain}. Check your key."
        return httpx.Response(401, json={"error": {"message": message, "code": "invalid_key"}})

    async with build(tmp_path, FakeUpstream(reject), upstream_key=plain) as client:
        await through(client)
        resp = await client.post("/v1/chat/completions", json=chat("will be rejected"))
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "semula_upstream_error"
        assert "401" in resp.json()["error"]["message"]
        assert plain not in resp.text
        assert plain not in (await client.get("/api/v1/log")).text


async def test_a_credential_shaped_string_in_a_provider_error_is_scrubbed_too(
    tmp_path: Path,
) -> None:
    masked = "sk-proj-" + "abcdefghijklmnopqrstuvwxyz0123456789"  # a key we were never given

    def reject(body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"bad key {masked}"}})

    async with build(tmp_path, FakeUpstream(reject)) as client:
        await through(client)
        resp = await client.post("/v1/chat/completions", json=chat("masked"))
        assert resp.status_code == 502 and masked not in resp.text


async def test_a_failed_call_costs_no_budget_and_saves_nothing(tmp_path: Path) -> None:
    def down(body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "overloaded"}})

    upstream = FakeUpstream(down)
    async with build(tmp_path, upstream) as client:
        await through(client, upstream_max_calls=1)
        for _ in range(3):
            resp = await client.post("/v1/chat/completions", json=chat("flaky"))
            assert resp.status_code == 502  # never the budget error: nothing was spent
        assert (await client.get("/api/v1/projects/default/entries")).json()["entries"] == []
        assert upstream.calls == 3


async def test_an_unreachable_upstream_is_a_clean_502(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    upstream = FakeUpstream()
    upstream.transport = httpx.MockTransport(refuse)
    async with build(tmp_path, upstream) as client:
        await through(client)
        resp = await client.post("/v1/chat/completions", json=chat("anyone there"))
        assert resp.status_code == 502 and "could not be reached" in resp.text


async def test_forwarding_is_off_by_default_and_a_miss_still_parks(
    tmp_path: Path, upstream: FakeUpstream
) -> None:
    async with build(tmp_path, upstream) as client:  # key set, project did not opt in
        resp = await client.post("/v1/chat/completions", json=chat("default off"))
        assert resp.status_code == 504 and resp.json()["error"]["code"] == "semula_record_timeout"
        assert len((await client.get("/api/v1/pending")).json()["pending"]) == 1
        assert upstream.calls == 0
        settings = (await client.get("/api/v1/projects/default")).json()["settings"]
        assert not settings.get("record_through")


async def test_replay_mode_fails_fast_and_never_forwards_even_when_opted_in(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    await client.patch("/api/v1/projects/default", json={"settings": {"mode": "replay"}})
    resp = await client.post("/v1/chat/completions", json=chat("replay only"))
    assert resp.status_code == 424 and resp.json()["error"]["code"] == "semula_no_match"
    assert upstream.calls == 0


async def test_switching_the_project_back_to_replay_stops_forwarding(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    await client.post("/v1/chat/completions", json=chat("one"))
    await client.patch("/api/v1/projects/default", json={"settings": {"mode": "replay"}})
    assert (await client.post("/v1/chat/completions", json=chat("two"))).status_code == 424
    assert (await client.post("/v1/chat/completions", json=chat("one"))).status_code == 200
    assert upstream.calls == 1


async def test_turning_it_on_needs_a_key_on_the_server(
    tmp_path: Path, upstream: FakeUpstream
) -> None:
    async with build(tmp_path, upstream, upstream_key=None) as client:
        resp = await client.patch(
            "/api/v1/projects/default", json={"settings": {"record_through": True}}
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "upstream_not_configured"


async def test_the_model_can_be_overridden_for_the_upstream(tmp_path: Path) -> None:
    upstream = FakeUpstream()
    async with build(tmp_path, upstream) as client:
        await through(client, upstream_model="gpt-real")
        resp = await client.post("/v1/chat/completions", json=chat("model swap"))
        assert upstream.bodies[0]["model"] == "gpt-real"
        assert resp.json()["model"] == "gpt-test"  # the app still sees the model it asked for


async def test_the_cap_stops_further_calls_with_a_clear_error(tmp_path: Path) -> None:
    upstream = FakeUpstream()
    async with build(tmp_path, upstream) as client:
        await through(client, upstream_max_calls=2)
        for prompt in ("a", "b"):
            assert (await client.post("/v1/chat/completions", json=chat(prompt))).status_code == 200
        over = await client.post("/v1/chat/completions", json=chat("c"))
        assert over.status_code == 429
        assert over.json()["error"]["code"] == "semula_upstream_budget"
        assert upstream.calls == 2
        # recorded answers still replay once the cap is spent
        assert (await client.post("/v1/chat/completions", json=chat("a"))).status_code == 200
        assert upstream.calls == 2


async def test_concurrent_misses_cannot_slip_past_the_cap(tmp_path: Path) -> None:
    upstream = FakeUpstream()
    async with build(tmp_path, upstream) as client:
        await through(client, upstream_max_calls=3)
        results = await asyncio.gather(
            *(client.post("/v1/chat/completions", json=chat(f"p{i}")) for i in range(10))
        )
        assert sorted(r.status_code for r in results) == [200] * 3 + [429] * 7
        assert upstream.calls == 3


async def test_a_cap_of_zero_forwards_nothing(tmp_path: Path) -> None:
    upstream = FakeUpstream()
    async with build(tmp_path, upstream) as client:
        await through(client, upstream_max_calls=0)
        assert (await client.post("/v1/chat/completions", json=chat("x"))).status_code == 429
        assert upstream.calls == 0


async def test_the_default_cap_is_finite(client: httpx.AsyncClient) -> None:
    settings = (await client.get("/api/v1/projects/default")).json()["settings"]
    assert settings.get("upstream_max_calls", 100) == 100


async def test_an_answer_that_calls_a_tool_the_request_did_not_offer_is_refused(
    tmp_path: Path,
) -> None:
    def bad_tool(body: dict[str, Any]) -> httpx.Response:
        call = {"id": "c1", "type": "function", "function": {"name": "nope", "arguments": "{}"}}
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}]}
        )

    async with build(tmp_path, FakeUpstream(bad_tool)) as client:
        await through(client)
        resp = await client.post("/v1/chat/completions", json=chat("tools"))
        assert resp.status_code == 422
        assert (await client.get("/api/v1/projects/default/entries")).json()["entries"] == []


async def test_a_bundle_never_carries_or_sets_the_forwarding_settings(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/v1/chat/completions", json=chat("export me"))
    bundle = (await client.get("/api/v1/projects/default/export")).json()
    settings = bundle["project"]["settings"]
    assert not {"record_through", "upstream_model", "upstream_max_calls", "mode"} & set(settings)
    bundle["project"]["id"] = "copy"
    bundle["entries"], bundle["aliases"] = [], []
    bundle["project"]["settings"] = {
        "record_through": True,
        "upstream_model": "x",
        "upstream_max_calls": 9999,
    }
    await client.patch("/api/v1/projects/default", json={"settings": {"record_through": None}})
    imported = await client.post("/api/v1/import", json={"bundle": bundle})
    assert imported.status_code in (200, 201), imported.text
    copy = (await client.get("/api/v1/projects/copy")).json()["settings"]
    assert not copy.get("record_through") and copy.get("upstream_model") is None
    assert copy.get("upstream_max_calls", 100) == 100


async def test_hosted_forwarding_is_off_unless_the_operator_allows_workspaces(
    tmp_path: Path, upstream: FakeUpstream
) -> None:
    for allowed, status in ((False, 403), (True, 200)):
        sub = tmp_path / str(allowed)
        sub.mkdir()
        settings = Settings(
            data_dir=sub,
            database_url=fresh_database_url(sub),
            hosted=True,
            mode="record",
            upstream_key=KEY,
            upstream_tenants=allowed,
        )
        app = create_app(settings, upstream_transport=upstream.transport)
        app.state.service.create_workspace("w")
        from semula.scope import Scope

        scoped = app.state.service.for_scope(Scope("w"))
        scoped.create_project("shop")
        try:
            scoped.update_project("shop", settings={"record_through": True})
            got = 200
        except Exception as exc:
            got = exc.status  # type: ignore[attr-defined]
        assert got == status
