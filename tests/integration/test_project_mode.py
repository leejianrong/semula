"""Mode is a per-project setting that survives a restart (SEM-73, ADR-0011)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from semula.cli import main
from tests.integration.conftest import chat, make_client
from tests.support.db import fresh_database_url
from tests.support.server import running_server

pytestmark = pytest.mark.integration


async def new_project(client: httpx.AsyncClient, slug: str) -> None:
    assert (await client.post("/api/v1/projects", json={"id": slug})).status_code == 201


async def set_project_mode(client: httpx.AsyncClient, slug: str, mode: str | None) -> Any:
    resp = await client.patch(f"/api/v1/projects/{slug}", json={"settings": {"mode": mode}})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def ask(client: httpx.AsyncClient, slug: str, prompt: str = "hi") -> httpx.Response:
    return await client.post(f"/v1/projects/{slug}/chat/completions", json=chat(prompt))


async def parked(client: httpx.AsyncClient, slug: str) -> list[dict]:
    resp = await client.get("/api/v1/pending", params={"project": slug})
    return resp.json()["pending"]  # type: ignore[no-any-return]


async def test_a_project_follows_the_server_default_until_it_chooses(
    replay: httpx.AsyncClient,
) -> None:
    await new_project(replay, "p")
    project = (await replay.get("/api/v1/projects/p")).json()
    assert project["mode"] == "replay" and project["settings"]["mode"] is None
    assert (await ask(replay, "p")).status_code == 424  # replay: nothing parked
    await replay.put("/api/v1/mode", json={"mode": "record"})  # the default moves...
    assert (await replay.get("/api/v1/projects/p")).json()["mode"] == "record"  # ...p follows
    await set_project_mode(replay, "p", "replay")  # now p has chosen
    await replay.put("/api/v1/mode", json={"mode": "record"})
    assert (await replay.get("/api/v1/projects/p")).json()["mode"] == "replay"


async def test_two_projects_can_be_in_different_modes_at_once(
    replay: httpx.AsyncClient,
) -> None:
    await new_project(replay, "rec")
    await new_project(replay, "rep")
    await set_project_mode(replay, "rec", "record")
    await set_project_mode(replay, "rep", "replay")
    held = asyncio.create_task(ask(replay, "rec", "park me"))
    for _ in range(300):
        if await parked(replay, "rec"):
            break
        await asyncio.sleep(0.01)
    assert len(await parked(replay, "rec")) == 1
    assert (await ask(replay, "rep", "park me")).status_code == 424
    assert await parked(replay, "rep") == []
    held.cancel()


async def test_a_projects_mode_survives_a_restart(tmp_path: Path) -> None:
    url = fresh_database_url(tmp_path)
    async with make_client(tmp_path, mode="replay", database_url=url) as first:
        await new_project(first, "keeps")
        await set_project_mode(first, "keeps", "record")
    async with make_client(tmp_path, mode="replay", database_url=url) as second:
        assert (await second.get("/api/v1/projects/keeps")).json()["mode"] == "record"
        assert (await second.get("/api/v1/mode")).json()["projects"]["keeps"] == "record"


async def test_the_global_switch_is_a_default_and_says_it_is_not_kept(
    replay: httpx.AsyncClient,
) -> None:
    resp = await replay.put("/api/v1/mode", json={"mode": "record"})
    assert resp.json() == {"mode": "record", "persisted": False, "project": None}
    assert (await replay.get("/api/v1/mode")).json()["mode"] == "record"


async def test_the_mode_endpoint_can_name_a_project_and_then_it_is_kept(
    tmp_path: Path,
) -> None:
    url = fresh_database_url(tmp_path)
    async with make_client(tmp_path, mode="replay", database_url=url) as first:
        await new_project(first, "p")
        resp = await first.put("/api/v1/mode", json={"mode": "record", "project": "p"})
        assert resp.json() == {"mode": "record", "persisted": True, "project": "p"}
        assert (
            await first.put("/api/v1/mode", json={"mode": "x", "project": "p"})
        ).status_code == 422
        assert (
            await first.put("/api/v1/mode", json={"mode": "record", "project": "no"})
        ).status_code == 404
    async with make_client(tmp_path, mode="replay", database_url=url) as second:
        assert (await second.get("/api/v1/projects/p")).json()["mode"] == "record"


async def test_clearing_a_projects_mode_makes_it_follow_the_default_again(
    replay: httpx.AsyncClient,
) -> None:
    await new_project(replay, "p")
    await set_project_mode(replay, "p", "record")
    after = await set_project_mode(replay, "p", None)
    assert after["mode"] == "replay" and after["settings"]["mode"] is None


async def test_a_bad_mode_in_settings_is_a_422(replay: httpx.AsyncClient) -> None:
    resp = await replay.patch("/api/v1/projects/default", json={"settings": {"mode": "chaos"}})
    assert resp.status_code == 422 and resp.json()["error"]["param"] == "settings.mode"


async def test_changing_the_mode_does_not_rekey_or_touch_recordings(
    replay: httpx.AsyncClient,
) -> None:
    made = await replay.post(
        "/api/v1/projects/default/entries",
        json={"request": chat("keep"), "response": {"content": "x"}},
    )
    before = (await replay.get(f"/api/v1/entries/{made.json()['id']}")).json()
    await set_project_mode(replay, "default", "record")
    after = (await replay.get(f"/api/v1/entries/{made.json()['id']}")).json()
    assert after["version"] == before["version"] and after["fingerprint"] == before["fingerprint"]
    assert (await replay.post("/v1/chat/completions", json=chat("keep"))).status_code == 200


async def test_each_call_is_logged_with_its_projects_mode(replay: httpx.AsyncClient) -> None:
    await new_project(replay, "p")
    await new_project(replay, "q")
    await set_project_mode(replay, "p", "record")  # p would park: use a short timeout
    await set_project_mode(replay, "q", "replay")
    await replay.patch("/api/v1/projects/p", json={"settings": {"record_timeout": 0.2}})
    await ask(replay, "p", "times out")
    await ask(replay, "q", "misses")
    modes = {c["project"]: c["mode"] for c in (await replay.get("/api/v1/log")).json()["calls"]}
    assert modes == {"p": "record", "q": "replay"}


async def test_test_match_reports_what_the_project_would_do(replay: httpx.AsyncClient) -> None:
    await new_project(replay, "p")
    url = "/api/v1/projects/p/test-match"
    assert (await replay.post(url, json={"prompt": "nothing"})).json()["would"] == "no_match_error"
    await set_project_mode(replay, "p", "record")
    out = (await replay.post(url, json={"prompt": "nothing"})).json()
    assert out["would"] == "park" and out["mode"] == "record"


async def test_the_mode_is_not_exported_and_an_import_cannot_set_it(
    replay: httpx.AsyncClient,
) -> None:
    await new_project(replay, "src")
    await set_project_mode(replay, "src", "record")
    bundle = (await replay.get("/api/v1/projects/src/export")).json()
    assert "mode" not in json.dumps(bundle["project"]["settings"])
    bundle["project"]["settings"]["mode"] = "record"  # a hand-edited or hostile bundle
    done = await replay.post("/api/v1/import", json={"bundle": bundle, "project": "copy"})
    assert done.status_code == 200, done.text
    assert (await replay.get("/api/v1/projects/copy")).json()["mode"] == "replay"


async def test_a_change_is_announced_with_its_project(replay: httpx.AsyncClient) -> None:
    await new_project(replay, "p")
    service = replay._transport.app.state.service  # type: ignore[attr-defined]
    with service.events.subscribe() as sub:
        await set_project_mode(replay, "p", "record")
        event = await asyncio.wait_for(sub.get(), 2)
        assert event == {"type": "mode", "project": "p", "mode": "record", "workspace": "default"}
        await replay.put("/api/v1/mode", json={"mode": "record"})
        event = await asyncio.wait_for(sub.get(), 2)
        assert event == {"type": "mode", "project": None, "mode": "record", "workspace": "default"}


async def test_the_listing_and_health_show_modes(replay: httpx.AsyncClient) -> None:
    await new_project(replay, "p")
    await set_project_mode(replay, "p", "record")
    listed = {p["id"]: p["mode"] for p in (await replay.get("/api/v1/projects")).json()["projects"]}
    assert listed == {"default": "replay", "p": "record"}
    assert (await replay.get("/health")).json()["mode"] == "replay"  # the server default


def test_the_cli_sets_a_projects_mode_and_says_when_it_is_not_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path, mode="replay") as url:
        httpx.post(url + "/api/v1/projects", json={"id": "p"})
        capsys.readouterr()
        assert main(["--url", url, "mode"]) == 0
        shown = capsys.readouterr().out
        assert "replay" in shown and "p" in shown
        assert main(["--url", url, "mode", "record", "--project", "p"]) == 0
        assert "record" in capsys.readouterr().out
        assert httpx.get(url + "/api/v1/projects/p").json()["mode"] == "record"
        assert main(["--url", url, "mode", "record"]) == 0
        assert "until the server restarts" in capsys.readouterr().out
        assert main(["--url", url, "mode", "record", "--project", "nope"]) == 3


def test_mcp_sets_and_reports_the_mode_per_project(tmp_path: Path) -> None:
    from fastmcp import Client

    async def go(url: str) -> None:
        async with Client(url + "/mcp/") as client:
            status = (await client.call_tool("get_status", {})).data
            assert status["mode"] == "replay"
            assert {p["id"]: p["mode"] for p in status["projects"]}["default"] == "replay"
            done = (
                await client.call_tool("set_mode", {"mode": "record", "project": "default"})
            ).data
            assert done["persisted"] is True and done["mode"] == "record"
            status = (await client.call_tool("get_status", {})).data
            assert {p["id"]: p["mode"] for p in status["projects"]}["default"] == "record"

    with running_server(tmp_path, mode="replay") as url:
        asyncio.run(go(url))
