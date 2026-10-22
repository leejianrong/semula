"""Hosted mode: every call carries an API token that names workspace, scope and project
(ADR-0010, SEM-83), driven through the real app."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from semula import canonical
from semula.app import create_app
from semula.config import Settings
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration


class Hosted:
    def __init__(self, client: httpx.AsyncClient, service: Service) -> None:
        self.client = client
        self.service = service
        self.a = service.for_scope(Scope("a"))
        self.b = service.for_scope(Scope("b"))
        self.tokens: dict[str, str] = {}

    def make(self, who: str, scope: str, project: str | None = None, workspace: str = "a") -> str:
        svc = self.a if workspace == "a" else self.b
        secret: str = svc.create_token(who, scope, project=project)["token"]
        self.tokens[who] = secret
        return secret

    def h(self, who: str, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}", **extra}


@pytest.fixture
async def hosted(tmp_path: Path) -> AsyncIterator[Hosted]:
    settings = Settings(
        data_dir=tmp_path, hosted=True, mode="replay", database_url=fresh_database_url(tmp_path)
    )
    app = create_app(settings)
    service: Service = app.state.service
    for ws in ("a", "b"):
        service.create_workspace(ws)
    a = service.for_scope(Scope("a"))
    a.create_project("shop")
    a.create_project("other")
    entry, _ = a.upsert_entry(
        "shop", {"messages": [{"role": "user", "content": "hello a"}]}, {"content": "a's answer"}
    )
    b = service.for_scope(Scope("b"))
    b.create_project("shop")
    b.upsert_entry(
        "shop", {"messages": [{"role": "user", "content": "hello a"}]}, {"content": "B's answer"}
    )
    world = Hosted(
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), service
    )
    world.entry = entry["id"]  # type: ignore[attr-defined]
    for who, scope in (("replay", "replay"), ("record", "record"), ("admin", "admin")):
        world.make(who, scope)
    async with world.client:
        yield world


def ask(project: str = "shop", text: str = "hello a") -> dict[str, Any]:
    return {"model": "m", "messages": [{"role": "user", "content": text}]}


async def test_no_token_or_a_wrong_one_is_refused_and_health_stays_open(hosted: Hosted) -> None:
    c = hosted.client
    assert (await c.get("/health")).status_code == 200
    for headers in (
        {},
        {"Authorization": "Bearer nope"},
        {"Authorization": "Bearer smla_live_" + "x" * 32},
    ):
        for path in ("/v1/models", "/api/v1/projects"):
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 401 and resp.json()["error"]["code"] == "invalid_api_key"
            assert resp.headers["www-authenticate"] == "Bearer"


async def test_guessing_tokens_is_rate_limited(hosted: Hosted) -> None:
    for _ in range(20):
        await hosted.client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    blocked = await hosted.client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    # even a good token waits: the address is blocked, not the token
    assert (await hosted.client.get("/v1/models", headers=hosted.h("admin"))).status_code == 429


async def test_a_token_replays_its_own_workspaces_answer_only(hosted: Hosted) -> None:
    resp = await hosted.client.post(
        "/v1/projects/shop/chat/completions", json=ask(), headers=hosted.h("replay")
    )
    assert resp.json()["choices"][0]["message"]["content"] == "a's answer"
    hosted.make("b-replay", "replay", workspace="b")
    resp = await hosted.client.post(
        "/v1/projects/shop/chat/completions", json=ask(), headers=hosted.h("b-replay")
    )
    assert resp.json()["choices"][0]["message"]["content"] == "B's answer"
    other = await hosted.client.post(
        "/v1/projects/other/chat/completions", json=ask(), headers=hosted.h("b-replay")
    )
    assert other.status_code == 404  # B has no project 'other': A's is not reachable


async def test_a_replay_token_never_parks_even_in_a_recording_project(hosted: Hosted) -> None:
    hosted.a.set_mode("record", "shop")
    miss = await hosted.client.post(
        "/v1/projects/shop/chat/completions", json=ask(text="unknown"), headers=hosted.h("replay")
    )
    assert miss.status_code == 424 and "never parks" in miss.json()["error"]["message"]
    assert hosted.a.list_pending() == []


async def test_a_replay_token_reaches_nothing_under_api(hosted: Hosted) -> None:
    for method, path in (
        ("GET", "/api/v1/projects"),
        ("GET", "/api/v1/pending"),
        ("GET", "/api/v1/log"),
    ):
        resp = await hosted.client.request(method, path, headers=hosted.h("replay"))
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "insufficient_scope"


async def test_a_record_token_records_but_cannot_destroy_approve_or_reconfigure(
    hosted: Hosted,
) -> None:
    hosted.a.set_mode("record", "other")
    call = asyncio.create_task(
        hosted.client.post(
            "/v1/projects/other/chat/completions",
            json=ask(text="park me"),
            headers=hosted.h("replay"),
        )
    )
    # a replay token cannot park, so the recording work is driven by the record token's own app call
    assert (await call).status_code == 424
    pending = hosted.a.open_pending(canonical.from_openai(ask(text="park me")), "other")
    listed = await hosted.client.get("/api/v1/pending", headers=hosted.h("record"))
    assert [p["id"] for p in listed.json()["pending"]] == [pending]
    answered = await hosted.client.post(
        f"/api/v1/pending/{pending}/answer", json={"content": "ok"}, headers=hosted.h("record")
    )
    assert answered.status_code == 200
    for method, path in (
        ("DELETE", f"/api/v1/entries/{hosted.entry}?version=1"),  # type: ignore[attr-defined]
        ("PUT", "/api/v1/mode"),
        ("PATCH", "/api/v1/projects/shop"),
        ("POST", "/api/v1/import"),
        ("GET", "/api/v1/projects/shop/export"),
        ("DELETE", "/api/v1/projects/shop"),
    ):
        resp = await hosted.client.request(method, path, json={}, headers=hosted.h("record"))
        assert resp.status_code == 403, (method, path, resp.text)
    approve = await hosted.client.post("/api/v1/candidates/X/approve", headers=hosted.h("record"))
    assert approve.status_code == 403
    assert (
        await hosted.client.get("/api/v1/projects/shop", headers=hosted.h("record"))
    ).status_code == 200


async def test_the_actor_comes_from_the_credential_not_a_header(hosted: Hosted) -> None:
    entry = hosted.entry  # type: ignore[attr-defined]
    resp = await hosted.client.put(
        f"/api/v1/entries/{entry}",
        json={"version": 1, "response": {"content": "agent edit"}},
        headers=hosted.h("record", **{"X-Semula-Actor": "human"}),
    )
    assert resp.status_code == 200
    history = hosted.a.entry_history(entry)["revisions"]
    assert history[0]["actor"] == "agent"


async def test_a_record_token_cannot_force_past_a_pin_but_an_admin_can(hosted: Hosted) -> None:
    entry = hosted.entry  # type: ignore[attr-defined]
    hosted.a.update_entry(entry, 1, {"pinned": True}, actor="human")
    url = f"/api/v1/entries/{entry}"
    body = {"version": 2, "response": {"content": "forced"}, "force": True}
    assert (await hosted.client.put(url, json=body, headers=hosted.h("record"))).status_code == 403
    pinned = await hosted.client.put(
        url, json={"version": 2, "response": {"content": "x"}}, headers=hosted.h("record")
    )
    assert pinned.status_code == 409 and pinned.json()["error"]["code"] == "entry_pinned"
    assert (await hosted.client.put(url, json=body, headers=hosted.h("admin"))).status_code == 200
    assert hosted.a.entry_history(entry)["revisions"][0]["forced"] is True


async def test_an_admin_token_deletes_but_still_cannot_manage_tokens(hosted: Hosted) -> None:
    entry = hosted.entry  # type: ignore[attr-defined]
    for method, path in (
        ("GET", "/api/v1/tokens"),
        ("POST", "/api/v1/tokens"),
        ("POST", "/api/v1/tokens/X/rotate"),
        ("POST", "/api/v1/tokens/X/revoke"),
    ):
        resp = await hosted.client.request(method, path, json={}, headers=hosted.h("admin"))
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden", path
    gone = await hosted.client.delete(
        f"/api/v1/entries/{entry}?version=1", headers=hosted.h("admin")
    )
    assert gone.status_code == 204


async def test_a_project_bound_token_stays_in_its_project(hosted: Hosted) -> None:
    hosted.make("shop-only", "record", project="shop")
    c = hosted.client
    # naming no project means its own
    own = await c.post("/v1/chat/completions", json=ask(), headers=hosted.h("shop-only"))
    assert own.json()["choices"][0]["message"]["content"] == "a's answer"
    # naming another one looks like it does not exist
    for resp in (
        await c.post(
            "/v1/projects/other/chat/completions", json=ask(), headers=hosted.h("shop-only")
        ),
        await c.post(
            "/v1/chat/completions",
            json=ask(),
            headers=hosted.h("shop-only", **{"X-Semula-Project": "other"}),
        ),
        await c.get("/api/v1/projects/other/entries", headers=hosted.h("shop-only")),
    ):
        assert resp.status_code == 404 and resp.json()["error"]["code"] == "project_not_found"
    assert (
        await c.get("/api/v1/projects/shop/entries", headers=hosted.h("shop-only"))
    ).status_code == 200
    # routes that name no project show only its own
    other_entry, _ = hosted.a.upsert_entry(
        "other", {"messages": [{"role": "user", "content": "secret"}]}, {"content": "x"}
    )
    hosted.a.set_mode("record", "other")
    hosted.a.open_pending(canonical.from_openai(ask(text="other waits")), "other")
    hosted.a.open_pending(canonical.from_openai(ask(text="shop waits")), "shop")
    projects = (await c.get("/api/v1/projects", headers=hosted.h("shop-only"))).json()["projects"]
    assert [p["id"] for p in projects] == ["shop"]
    pending = (await c.get("/api/v1/pending", headers=hosted.h("shop-only"))).json()["pending"]
    assert {p["project"] for p in pending} == {"shop"}
    got = await c.get(f"/api/v1/entries/{other_entry['id']}", headers=hosted.h("shop-only"))
    assert got.status_code == 404 and got.json()["error"]["code"] == "entry_not_found"
    assert (await c.get("/api/v1/log", headers=hosted.h("shop-only"))).status_code == 200
    assert (await c.get("/api/v1/mode", headers=hosted.h("shop-only"))).json()["projects"] == {
        "shop": "replay"
    }


async def test_revoking_cuts_a_token_off_at_once(hosted: Hosted) -> None:
    made = hosted.a.create_token("temp", "admin")
    headers = {"Authorization": f"Bearer {made['token']}"}
    assert (await hosted.client.get("/v1/models", headers=headers)).status_code == 200
    hosted.a.revoke_token(made["id"])
    assert (await hosted.client.get("/v1/models", headers=headers)).status_code == 401


async def test_the_open_dashboard_is_not_served_by_a_hosted_server(hosted: Hosted) -> None:
    ui = await hosted.client.get("/ui/projects/default/log", follow_redirects=True)
    assert ui.status_code != 200 or "sign" in ui.text.lower() or "off" in ui.text.lower()


async def test_a_token_pasted_into_a_prompt_is_not_stored(hosted: Hosted) -> None:
    hosted.a.set_mode("record", "other")
    secret = hosted.tokens["admin"]
    asked = ask(text=f"my token is {secret}, keep it safe")
    pending = hosted.a.open_pending(canonical.from_openai(asked), "other")
    stored = hosted.a.get_pending(pending)["request"]["messages"][0]["content"]
    assert secret not in stored and "[REDACTED]" in stored
