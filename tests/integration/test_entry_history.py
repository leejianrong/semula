"""Entry history: a snapshot per write, a cap, and Compare data for the conflict banner (SEM-66)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

from semula.cli import main
from tests.integration.conftest import chat
from tests.integration.test_entries import HUMAN
from tests.support.server import running_server

pytestmark = pytest.mark.integration

URL = "/api/v1/projects/default/entries"


async def author(client: httpx.AsyncClient, prompt: str = "history", answer: str = "v1") -> dict:
    resp = await client.post(URL, json={"request": chat(prompt), "response": {"content": answer}})
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def edit(
    client: httpx.AsyncClient, entry: dict, headers: dict | None = None, **changes: Any
) -> httpx.Response:
    return await client.put(
        f"/api/v1/entries/{entry['id']}",
        json={"version": entry["version"], **changes},
        headers=headers,
    )


async def history(client: httpx.AsyncClient, entry_id: str, **params: object) -> dict:
    resp = await client.get(f"/api/v1/entries/{entry_id}/history", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


def text_of(snapshot: dict) -> list[str]:
    return [r["message"]["content"] for r in snapshot["responses"]]


async def test_a_new_entry_starts_its_history_at_version_one(replay: httpx.AsyncClient) -> None:
    entry = await author(replay)
    out = await history(replay, entry["id"])
    assert out["entry_id"] == entry["id"] and out["version"] == 1
    (first,) = out["revisions"]
    assert first["version"] == 1 and first["actor"] == "agent" and first["summary"] == "created"
    assert first["forced"] is False and first["ts"].endswith("Z")
    assert "snapshot" not in first  # the list is lean


async def test_every_edit_adds_a_revision_with_actor_summary_and_snapshot(
    replay: httpx.AsyncClient,
) -> None:
    entry = await author(replay)
    second = (await edit(replay, entry, HUMAN, response={"content": "v2"})).json()
    third = (await edit(replay, second, add_response={"content": "alt"})).json()
    fourth = (await edit(replay, third, HUMAN, selection="cycle", pinned=True)).json()
    out = await history(replay, entry["id"])
    assert [(r["version"], r["actor"], r["summary"]) for r in out["revisions"]] == [
        (4, "human", "selection first to cycle, pinned"),
        (3, "agent", "answer added (2 in all)"),
        (2, "human", "answer replaced"),
        (1, "agent", "created"),
    ]  # newest first
    assert fourth["version"] == 4 == out["version"]
    one = (await history(replay, entry["id"], version=1))["revision"]["snapshot"]
    assert text_of(one) == ["v1"] and one["selection"] == "first" and one["pinned"] is False
    two = (await history(replay, entry["id"], version=2))["revision"]
    assert text_of(two["snapshot"]) == ["v2"] and two["actor"] == "human"
    last = (await history(replay, entry["id"], version=4))["revision"]["snapshot"]
    assert text_of(last) == ["v2", "alt"] and last["selection"] == "cycle" and last["pinned"]
    assert last == {
        "responses": fourth["responses"],
        "selection": "cycle",
        "pinned": True,
    }  # fmt: skip  (the snapshot is the entry's own state)


async def test_pinning_and_unpinning_are_named(replay: httpx.AsyncClient) -> None:
    entry = await author(replay)
    pinned = (await edit(replay, entry, HUMAN, pinned=True)).json()
    await edit(replay, pinned, HUMAN, pinned=False)
    summaries = [r["summary"] for r in (await history(replay, entry["id"]))["revisions"]]
    assert summaries[:2] == ["unpinned", "pinned"]


async def test_an_answer_from_the_queue_is_recorded_by_whoever_gave_it(
    record: httpx.AsyncClient,
) -> None:
    async def park(prompt: str) -> tuple[asyncio.Task, str]:
        call = asyncio.create_task(record.post("/v1/chat/completions", json=chat(prompt)))
        for _ in range(300):
            pending = (await record.get("/api/v1/pending")).json()["pending"]
            mine = [p for p in pending if p["last_message"] == prompt]
            if mine:
                return call, mine[0]["id"]
            await asyncio.sleep(0.01)
        raise AssertionError("never parked")

    for prompt, headers, actor in (("by agent", None, "agent"), ("by human", HUMAN, "human")):
        call, pending_id = await park(prompt)
        done = await record.post(
            f"/api/v1/pending/{pending_id}/answer", json={"content": "ok"}, headers=headers
        )
        await call
        (rev,) = (await history(record, done.json()["entry_id"]))["revisions"]
        assert (rev["version"], rev["actor"], rev["summary"]) == (1, actor, "recorded")


async def test_a_forced_agent_write_on_a_pinned_entry_is_marked_forced(
    replay: httpx.AsyncClient,
) -> None:
    entry = await author(replay)
    pinned = (
        await edit(replay, entry, HUMAN, pinned=True, response={"content": "human fix"})
    ).json()
    refused = await edit(replay, pinned, response={"content": "agent overwrite"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "entry_pinned"
    assert [r["version"] for r in (await history(replay, entry["id"]))["revisions"]] == [2, 1]
    forced = await edit(replay, pinned, force=True, response={"content": "agent overwrite"})
    assert forced.status_code == 200
    out = await history(replay, entry["id"])
    top = out["revisions"][0]
    assert (top["version"], top["actor"], top["forced"]) == (3, "agent", True)
    assert [r["forced"] for r in out["revisions"][1:]] == [False, False]
    snap = (await history(replay, entry["id"], version=3))["revision"]["snapshot"]
    assert text_of(snap) == ["agent overwrite"] and snap["pinned"] is True  # still pinned
    # what the human had written is still there to compare against
    before = (await history(replay, entry["id"], version=2))["revision"]["snapshot"]
    assert text_of(before) == ["human fix"]


async def test_an_upsert_over_an_existing_entry_is_a_revision_too(
    replay: httpx.AsyncClient,
) -> None:
    entry = await author(replay, "again")
    resp = await replay.post(
        URL,
        json={"request": chat("again"), "response": {"content": "v2"}, "version": entry["version"]},
    )
    assert resp.status_code == 200
    out = await history(replay, entry["id"])
    assert [(r["version"], r["summary"]) for r in out["revisions"]] == [
        (2, "answer replaced"),
        (1, "created"),
    ]


async def test_refused_writes_leave_no_revision(replay: httpx.AsyncClient) -> None:
    entry = await author(replay)
    stale = await replay.put(
        f"/api/v1/entries/{entry['id']}", json={"version": 7, "response": {"content": "x"}}
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    assert stale.json()["current"]["id"] == entry["id"]  # the banner still gets the current entry
    assert stale.json()["current"]["version"] == 1
    for bad in (
        {"version": 1, "response": {"nope": 1}},  # invalid answer
        {"version": 1, "selection": "random"},
        {"version": 1, "pinned": "yes"},
        {"version": 1},  # nothing to change
        {"version": 1, "mystery": 1},
    ):
        assert (await replay.put(f"/api/v1/entries/{entry['id']}", json=bad)).status_code >= 400
    out = await history(replay, entry["id"])
    assert [r["version"] for r in out["revisions"]] == [1] and out["version"] == 1


async def test_two_writers_at_one_version_make_one_revision(replay: httpx.AsyncClient) -> None:
    entry = await author(replay)
    results = await asyncio.gather(
        edit(replay, entry, response={"content": "a"}),
        edit(replay, entry, response={"content": "b"}),
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    out = await history(replay, entry["id"])
    assert [r["version"] for r in out["revisions"]] == [2, 1]


async def test_a_failure_while_recording_the_revision_undoes_the_edit(
    replay: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revision is written in the same transaction as the edit, so neither survives alone."""
    entry = await author(replay)
    service = replay._transport.app.state.service  # type: ignore[attr-defined]

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(service, "_revise", boom)
    with pytest.raises(RuntimeError):
        await edit(replay, entry, response={"content": "lost"})
    monkeypatch.undo()
    now = (await replay.get(f"/api/v1/entries/{entry['id']}")).json()
    assert now["version"] == 1 and now["responses"][0]["message"]["content"] == "v1"
    assert [r["version"] for r in (await history(replay, entry["id"]))["revisions"]] == [1]


async def test_only_the_latest_fifty_revisions_are_kept(replay: httpx.AsyncClient) -> None:
    entry = await author(replay)
    for n in range(2, 57):
        entry = (await edit(replay, entry, response={"content": f"v{n}"})).json()
    assert entry["version"] == 56
    out = await history(replay, entry["id"])
    versions = [r["version"] for r in out["revisions"]]
    assert versions == list(range(56, 6, -1)) and len(versions) == 50  # 7..56 kept
    assert (await replay.get(f"/api/v1/entries/{entry['id']}/history?version=6")).status_code == 404
    assert text_of((await history(replay, entry["id"], version=7))["revision"]["snapshot"]) == [
        "v7"
    ]


async def test_reads_do_not_make_revisions(replay: httpx.AsyncClient) -> None:
    entry = await author(replay)
    for _ in range(3):
        assert (await replay.post("/v1/chat/completions", json=chat("history"))).status_code == 200
    assert (await replay.get(f"/api/v1/entries/{entry['id']}")).json()["hit_count"] == 3
    assert [r["version"] for r in (await history(replay, entry["id"]))["revisions"]] == [1]
    await replay.patch("/api/v1/projects/default", json={"settings": {"tail_n": 3}})  # a re-key
    assert [r["version"] for r in (await history(replay, entry["id"]))["revisions"]] == [1]


async def test_deleting_an_entry_deletes_its_history(replay: httpx.AsyncClient) -> None:
    entry = await author(replay)
    await edit(replay, entry, response={"content": "v2"})
    db = replay._transport.app.state.db  # type: ignore[attr-defined]
    assert db.query_one("SELECT COUNT(*) AS n FROM entry_revision")["n"] == 2
    gone = await replay.delete(f"/api/v1/entries/{entry['id']}", params={"version": 2})
    assert gone.status_code == 204
    assert db.query_one("SELECT COUNT(*) AS n FROM entry_revision")["n"] == 0
    assert (await replay.get(f"/api/v1/entries/{entry['id']}/history")).status_code == 404


async def test_deleting_a_project_deletes_its_history(replay: httpx.AsyncClient) -> None:
    await replay.post("/api/v1/projects", json={"id": "scratch"})
    made = await replay.post(
        "/api/v1/projects/scratch/entries",
        json={"request": chat("x"), "response": {"content": "y"}},
    )
    assert made.status_code == 201
    assert (await replay.delete("/api/v1/projects/scratch")).status_code == 204
    db = replay._transport.app.state.db  # type: ignore[attr-defined]
    assert db.query_one("SELECT COUNT(*) AS n FROM entry_revision")["n"] == 0


async def test_bad_lookups(replay: httpx.AsyncClient) -> None:
    entry = await author(replay)
    base = f"/api/v1/entries/{entry['id']}/history"
    assert (await replay.get("/api/v1/entries/nope/history")).status_code == 404
    missing = await replay.get(base, params={"version": 9})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "revision_not_found"
    assert (await replay.get(base, params={"version": "abc"})).status_code == 422


async def test_export_carries_no_history(
    replay: httpx.AsyncClient,
) -> None:
    entry = await author(replay, "an entry to export")
    await edit(replay, entry, response={"content": "v2"})
    bundle = (await replay.get("/api/v1/projects/default/export")).json()
    assert "history" not in json.dumps(bundle) and "revision" not in json.dumps(bundle)
    assert "summary" not in json.dumps(bundle) and "snapshot" not in json.dumps(bundle)
    assert set(bundle["entries"][0]) <= {
        "id", "version", "request", "responses", "selection", "pinned", "created_at", "updated_at",
    }  # fmt: skip


async def test_import_records_a_revision_for_what_it_creates_and_changes(
    tmp_path: Path,
) -> None:
    from tests.integration.conftest import make_client

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    async with make_client(tmp_path / "a", mode="replay") as src:
        entry = await author(src)
        await edit(src, entry, response={"content": "v2"})
        bundle = (await src.get("/api/v1/projects/default/export")).json()
    async with make_client(tmp_path / "b", mode="replay") as dst:
        assert (
            await dst.post("/api/v1/import", json={"bundle": bundle, "project": "default"})
        ).status_code == 200
        (imported,) = (await dst.get(URL)).json()["entries"]
        out = await history(dst, imported["id"])
        assert [(r["version"], r["summary"]) for r in out["revisions"]] == [(2, "imported")]
        again = await dst.post("/api/v1/import", json={"bundle": bundle, "project": "default"})
        assert again.json()["entries"]["unchanged"] == 1
        assert len((await history(dst, imported["id"]))["revisions"]) == 1  # nothing changed
        bundle["entries"][0]["responses"] = [{"message": {"role": "assistant", "content": "v3"}}]
        assert (
            await dst.post("/api/v1/import", json={"bundle": bundle, "project": "default"})
        ).status_code == 200
        out = await history(dst, imported["id"])
        assert [(r["version"], r["summary"]) for r in out["revisions"]] == [
            (3, "imported, answers replaced"),
            (2, "imported"),
        ]


def test_the_cli_and_mcp_read_history(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    async def mcp(url: str, entry_id: str) -> Any:
        async with Client(url + "/mcp/") as client:
            listed = await client.call_tool("entry_history", {"id": entry_id})
            one = await client.call_tool("entry_history", {"id": entry_id, "version": 1})
            return [
                r.data if r.data is not None else json.loads(r.content[0].text)
                for r in (listed, one)
            ]

    with running_server(tmp_path, mode="replay") as url:
        made = httpx.post(
            url + URL, json={"request": chat("cli"), "response": {"content": "one"}}
        ).json()
        httpx.put(
            f"{url}/api/v1/entries/{made['id']}",
            json={"version": 1, "response": {"content": "two"}},
            headers=HUMAN,
        )
        capsys.readouterr()
        assert main(["--url", url, "entry", "history", made["id"]]) == 0
        human = capsys.readouterr().out
        assert "v2" in human and "human" in human and "answer replaced" in human and "v1" in human
        assert main(["--url", url, "entry", "history", made["id"], "--version", "1", "--json"]) == 0
        shown = json.loads(capsys.readouterr().out)
        assert shown["revision"]["snapshot"]["responses"][0]["message"]["content"] == "one"
        assert main(["--url", url, "entry", "history", made["id"], "--version", "1"]) == 0
        assert "one" in capsys.readouterr().out
        assert main(["--url", url, "entry", "history", made["id"], "--version", "9"]) == 3
        listed, one = asyncio.run(mcp(url, made["id"]))
        assert [r["version"] for r in listed["revisions"]] == [2, 1]
        assert one["revision"]["snapshot"]["selection"] == "first"
