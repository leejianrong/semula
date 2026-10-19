"""Export a project to files and import it back, losslessly and safely (SEM-37)."""

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from semula.cli import main
from tests.integration.conftest import chat, make_client
from tests.integration.test_lexical_match import BASE, WEAK, ask
from tests.integration.test_redaction import KEY
from tests.support.server import running_server

pytestmark = pytest.mark.integration

TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


async def put(client: httpx.AsyncClient, request: dict, response: Any, **extra: Any) -> dict:
    resp = await client.post(
        "/api/v1/projects/shop/entries", json={"request": request, "response": response, **extra}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def build_project(client: httpx.AsyncClient) -> None:
    """A project that uses everything an export has to carry."""
    await client.post("/api/v1/projects", json={"id": "shop", "name": "The Shop"})
    await client.patch(
        "/api/v1/projects/shop",
        json={"settings": {"tail_n": 4, "scrub_patterns": [r"order-\d+"]}},
    )
    await put(client, chat("plain question"), {"content": "plain answer"})
    await put(
        client,
        {**chat("weather in Paris?"), "tools": [TOOL]},
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}
                ],
            }
        },
    )
    streamed = await put(client, chat("tell me a story"), {"chunks": ["Once ", "upon ", "a time"]})
    many = await put(client, chat("pick a number"), {"content": "one"})
    await client.put(
        f"/api/v1/entries/{many['id']}",
        json={"version": 1, "add_response": {"content": "two"}, "selection": "cycle"},
    )
    pinned = (await client.get(f"/api/v1/entries/{many['id']}")).json()
    await client.put(
        f"/api/v1/entries/{streamed['id']}",
        json={"version": 1, "pinned": True},
        headers={"X-Semula-Actor": "human"},
    )
    assert pinned["version"] == 2
    # an approved near-miss, so an alias is exported too
    await put(client, ask(BASE), {"content": "line 1: use +"})
    await client.post("/v1/projects/shop/chat/completions", json=ask(WEAK))
    (found,) = (await client.get("/api/v1/candidates")).json()["candidates"]
    await client.post(f"/api/v1/candidates/{found['id']}/approve")


async def export(client: httpx.AsyncClient, project: str = "shop") -> dict:
    resp = await client.get(f"/api/v1/projects/{project}/export")
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def do_import(client: httpx.AsyncClient, bundle: dict, **extra: Any) -> httpx.Response:
    return await client.post("/api/v1/import", json={"bundle": bundle, **extra})


async def test_a_round_trip_into_a_fresh_server_is_lossless(tmp_path: Path) -> None:
    async with make_client(tmp_path / "a", mode="replay") as a:
        await build_project(a)
        first = await export(a)
    assert first["format"] == "semula/1" and len(first["entries"]) == 5
    assert len(first["aliases"]) == 1
    assert first["project"]["settings"] == {"tail_n": 4, "scrub_patterns": [r"order-\d+"]}

    async with make_client(tmp_path / "b", mode="replay") as b:
        done = await do_import(b, first)
        assert done.status_code == 200, done.text
        report = done.json()
        assert report["created_project"] is True
        assert report["entries"]["created"] == 5 and report["aliases"]["created"] == 1
        assert await export(b) == first  # everything authored survives, ids and versions too
        # and it replays: exact, tool call, stream, cycle, and the approved alias
        hit = await b.post("/v1/projects/shop/chat/completions", json=chat("plain question"))
        assert hit.json()["choices"][0]["message"]["content"] == "plain answer"
        tools = await b.post(
            "/v1/projects/shop/chat/completions",
            json={**chat("weather in Paris?"), "tools": [TOOL]},
        )
        assert tools.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == (
            "get_weather"
        )
        alias = await b.post("/v1/projects/shop/chat/completions", json=ask(WEAK))
        assert alias.json()["choices"][0]["message"]["content"] == "line 1: use +"
        calls = (await b.get("/api/v1/log", params={"outcome": "hit"})).json()["calls"]
        assert calls[0]["tier"] == "alias"
        streamed = await b.post(
            "/v1/projects/shop/chat/completions",
            json={**chat("tell me a story"), "stream": True},
        )
        assert "upon " in streamed.text  # the chunk boundaries came back too


async def test_importing_twice_changes_nothing(tmp_path: Path) -> None:
    async with make_client(tmp_path / "a", mode="replay") as a:
        await build_project(a)
        bundle = await export(a)
    async with make_client(tmp_path / "b", mode="replay") as b:
        await do_import(b, bundle)
        again = (await do_import(b, bundle)).json()
        assert not again["created_project"]
        assert again["entries"] == {
            "created": 0, "updated": 0, "unchanged": 5, "skipped_pinned": [],
        }  # fmt: skip
        assert again["aliases"] == {"created": 0, "updated": 0, "unchanged": 1}
        assert await export(b) == bundle  # versions did not move


async def test_a_changed_file_updates_the_entry_and_a_pin_holds_unless_forced(
    tmp_path: Path,
) -> None:
    async with make_client(tmp_path / "a", mode="replay") as a:
        await build_project(a)
        bundle = await export(a)
    async with make_client(tmp_path / "b", mode="replay") as b:
        await do_import(b, bundle)
        edited = copy.deepcopy(bundle)
        by_text = {e["request"]["messages"][-1]["content"]: e for e in edited["entries"]}
        by_text["plain question"]["responses"] = [
            {"message": {"role": "assistant", "content": "edited in git"}, "finish_reason": "stop"}
        ]
        by_text["tell me a story"]["responses"] = [
            {"message": {"role": "assistant", "content": "pinned edit"}, "finish_reason": "stop"}
        ]
        report = (await do_import(b, edited)).json()["entries"]
        assert report["updated"] == 1 and report["unchanged"] == 3
        assert report["skipped_pinned"] == [by_text["tell me a story"]["id"]]
        got = await b.post("/v1/projects/shop/chat/completions", json=chat("plain question"))
        assert got.json()["choices"][0]["message"]["content"] == "edited in git"
        entry = (await b.get(f"/api/v1/entries/{by_text['plain question']['id']}")).json()
        assert entry["version"] == by_text["plain question"]["version"] + 1
        forced = (await do_import(b, edited, force=True)).json()["entries"]
        assert forced["updated"] == 1 and forced["skipped_pinned"] == []


async def test_a_project_can_be_imported_under_another_id(tmp_path: Path) -> None:
    async with make_client(tmp_path / "a", mode="replay") as a:
        await build_project(a)
        bundle = await export(a)
    async with make_client(tmp_path / "b", mode="replay") as b:
        done = await do_import(b, bundle, project="copy")
        assert done.json()["project"] == "copy"
        hit = await b.post("/v1/projects/copy/chat/completions", json=chat("plain question"))
        assert hit.status_code == 200
        # the same ULIDs cannot live in two projects
        clash = await do_import(b, bundle, project="copy2")
        assert clash.status_code == 409
        assert clash.json()["error"]["code"] == "entry_in_other_project"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda b: b.update(format="semula/9"), "invalid_bundle"),
        (lambda b: b.pop("project"), "invalid_bundle"),
        (lambda b: b["entries"][0].pop("responses"), "invalid_bundle"),
        (lambda b: b["entries"][0].update(responses=[{"nonsense": 1}]), "invalid_bundle"),
        (lambda b: b["entries"][0].update(selection="random"), "invalid_bundle"),
        (lambda b: b["aliases"][0].update(entry_id="01MISSING"), "invalid_bundle"),
        (lambda b: b["project"].update(settings={"tail_n": 0}), "invalid_bundle"),
    ],
)
async def test_a_bad_bundle_is_rejected_and_nothing_is_written(
    tmp_path: Path, mutate: Any, code: str
) -> None:
    async with make_client(tmp_path / "a", mode="replay") as a:
        await build_project(a)
        bundle = await export(a)
    broken = copy.deepcopy(bundle)
    mutate(broken)
    async with make_client(tmp_path / "b", mode="replay") as b:
        resp = await do_import(b, broken)
        assert resp.status_code == 422 and resp.json()["error"]["code"] == code
        assert (await b.get("/api/v1/projects/shop")).status_code == 404  # not even the project


async def test_secrets_in_a_hand_edited_file_are_redacted_on_the_way_in(
    replay: httpx.AsyncClient,
) -> None:
    bundle = {
        "format": "semula/1",
        "project": {"id": "default"},
        "entries": [
            {
                "id": "01HZZZZZZZZZZZZZZZZZZZZZZZ",
                "request": {"messages": [{"role": "user", "content": f"key {KEY}"}], "tools": []},
                "responses": [{"message": {"role": "assistant", "content": "ok"}}],
            }
        ],
    }
    assert (await do_import(replay, bundle)).status_code == 200
    from tests.integration.test_call_log import every_stored_value

    assert KEY not in every_stored_value(replay)
    assert (await replay.post("/v1/chat/completions", json=chat(f"key {KEY}"))).status_code == 200


# --- files and the CLI -------------------------------------------------------------------
def test_the_cli_writes_diffable_files_and_a_second_server_reads_them_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "cassettes"

    def run(url: str, *argv: str) -> int:
        return main(["--url", url, *argv])

    with running_server(tmp_path / "a", mode="replay") as url:
        httpx.post(url + "/api/v1/projects", json={"id": "shop"})
        ids = []
        for text in ("alpha", "beta"):
            resp = httpx.post(
                url + "/api/v1/projects/shop/entries",
                json={"request": chat(text), "response": {"content": text.upper()}},
            )
            ids.append(resp.json()["id"])
        assert run(url, "export", "shop", "--dir", str(out), "--json") == 0
        assert json.loads(capsys.readouterr().out)["entries"] == 2
        first = {p.relative_to(out): p.read_bytes() for p in sorted(out.rglob("*.json"))}
        assert set(first) == {
            Path("semula.json"),
            *(Path("entries") / f"{i}.json" for i in ids),
        }
        assert first[Path("semula.json")].endswith(b"\n")
        assert '"format": "semula/1"' in first[Path("semula.json")].decode()

        assert run(url, "export", "shop", "--dir", str(out)) == 0  # again: nothing changes
        assert {p.relative_to(out): p.read_bytes() for p in sorted(out.rglob("*.json"))} == first

        entry = httpx.get(url + f"/api/v1/entries/{ids[0]}").json()
        httpx.delete(url + f"/api/v1/entries/{ids[0]}", params={"version": entry["version"]})
        assert run(url, "export", "shop", "--dir", str(out)) == 0
        assert not (out / "entries" / f"{ids[0]}.json").exists()  # a deleted entry leaves git
        assert (out / "entries" / f"{ids[1]}.json").exists()
        assert run(url, "export", "nope", "--dir", str(out)) == 3

    capsys.readouterr()
    with running_server(tmp_path / "b", mode="replay") as url:
        assert run(url, "import", str(out)) == 0
        assert "created" in capsys.readouterr().out
        assert run(url, "import", str(out), "--json") == 0
        assert json.loads(capsys.readouterr().out)["entries"]["unchanged"] == 1
        hit = httpx.post(url + "/v1/projects/shop/chat/completions", json=chat("beta"))
        assert hit.json()["choices"][0]["message"]["content"] == "BETA"
        assert run(url, "import", str(tmp_path / "missing")) == 4
