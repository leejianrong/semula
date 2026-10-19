import httpx
import pytest

from tests.integration.conftest import chat
from tests.integration.test_replay import record_answer

pytestmark = pytest.mark.integration

HUMAN = {"X-Semula-Actor": "human"}


async def make_entry(
    client: httpx.AsyncClient, prompt: str = "edit me", answer: str = "v1"
) -> dict:
    await record_answer(client, chat(prompt), {"content": answer})
    listed = (await client.get("/api/v1/projects/default/entries")).json()["entries"]
    entry = [e for e in listed if prompt in e["last_message"]][0]
    return (await client.get(f"/api/v1/entries/{entry['id']}")).json()  # type: ignore[no-any-return]


async def test_list_is_lean_and_get_is_full(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record)
    listed = (await record.get("/api/v1/projects/default/entries")).json()
    assert listed["total"] == 1
    row = listed["entries"][0]
    assert "request" not in row and isinstance(row["responses"], int)
    assert row["last_message"] == "edit me" and row["version"] == 1
    assert entry["request"]["messages"][-1]["content"] == "edit me"
    assert entry["responses"][0]["message"]["content"] == "v1"


async def test_list_filters_by_text_and_pages(record: httpx.AsyncClient) -> None:
    for word in ("alpha", "beta", "gamma"):
        await make_entry(record, word, word)
    found = (await record.get("/api/v1/projects/default/entries", params={"q": "BETA"})).json()
    assert [e["last_message"] for e in found["entries"]] == ["beta"]
    page = (await record.get("/api/v1/projects/default/entries", params={"limit": 2})).json()
    assert page["total"] == 3 and len(page["entries"]) == 2
    assert (await record.get("/api/v1/projects/ghost/entries")).status_code == 404


async def test_edit_replays_the_new_answer_and_bumps_the_version(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record)
    resp = await record.put(
        f"/api/v1/entries/{entry['id']}", json={"version": 1, "response": {"content": "v2"}}
    )
    assert resp.status_code == 200 and resp.json()["version"] == 2
    replay = await record.post("/v1/chat/completions", json=chat("edit me"))
    assert replay.json()["choices"][0]["message"]["content"] == "v2"


async def test_stale_write_is_rejected_with_the_current_entry(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record)
    url = f"/api/v1/entries/{entry['id']}"
    await record.put(url, json={"version": 1, "response": {"content": "human edit"}})
    stale = await record.put(url, json={"version": 1, "response": {"content": "agent edit"}})
    assert stale.status_code == 409
    body = stale.json()
    assert body["error"]["code"] == "version_conflict"
    assert body["current"]["version"] == 2
    assert body["current"]["responses"][0]["message"]["content"] == "human edit"
    # nothing was lost: the stored answer is still the first writer's
    assert (await record.get(url)).json()["responses"][0]["message"]["content"] == "human edit"


async def test_writes_must_state_a_version(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record)
    resp = await record.put(f"/api/v1/entries/{entry['id']}", json={"response": {"content": "x"}})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "version_required"
    assert (await record.delete(f"/api/v1/entries/{entry['id']}")).status_code == 400


async def test_invalid_edits_are_rejected_and_change_nothing(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record)
    url = f"/api/v1/entries/{entry['id']}"
    bad = await record.put(url, json={"version": 1, "response": {"content": 5}})
    assert bad.status_code == 422
    assert (await record.put(url, json={"version": 1})).status_code == 400
    assert (await record.put(url, json={"version": 1, "colour": "red"})).status_code == 400
    assert (await record.get(url)).json()["version"] == 1


async def test_delete_with_the_right_version(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record)
    url = f"/api/v1/entries/{entry['id']}"
    assert (await record.delete(url, params={"version": 7})).status_code == 409
    assert (await record.delete(url, params={"version": 1})).status_code == 204
    assert (await record.get(url)).status_code == 404


# --- pinning (SEM-20) -----------------------------------------------------------------


async def test_agents_cannot_overwrite_a_pinned_entry(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record)
    url = f"/api/v1/entries/{entry['id']}"
    pinned = await record.put(url, json={"version": 1, "pinned": True}, headers=HUMAN)
    assert pinned.json()["pinned"] is True

    blocked = await record.put(url, json={"version": 2, "response": {"content": "agent"}})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "entry_pinned"
    unpin = await record.put(url, json={"version": 2, "pinned": False})
    assert unpin.status_code == 409
    delete = await record.delete(url, params={"version": 2})
    assert delete.status_code == 409
    assert (await record.get(url)).json()["responses"][0]["message"]["content"] == "v1"


async def test_humans_can_edit_pinned_entries(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record)
    url = f"/api/v1/entries/{entry['id']}"
    await record.put(url, json={"version": 1, "pinned": True}, headers=HUMAN)
    ok = await record.put(url, json={"version": 2, "response": {"content": "human"}}, headers=HUMAN)
    assert ok.status_code == 200 and ok.json()["pinned"] is True


async def test_force_overrides_a_pin_and_is_logged(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record)
    url = f"/api/v1/entries/{entry['id']}"
    await record.put(url, json={"version": 1, "pinned": True}, headers=HUMAN)
    forced = await record.put(
        url, json={"version": 2, "response": {"content": "forced"}, "force": True}
    )
    assert forced.status_code == 200
    db = record._transport.app.state.db  # type: ignore[attr-defined]
    assert db.query_one("SELECT COUNT(*) AS n FROM call_log WHERE outcome='forced_write'")["n"] == 1
    assert (await record.delete(url, params={"version": 3, "force": "true"})).status_code == 204
    assert (
        db.query_one("SELECT COUNT(*) AS n FROM call_log WHERE outcome='forced_delete'")["n"] == 1
    )


async def test_agents_may_pin(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record)
    resp = await record.put(f"/api/v1/entries/{entry['id']}", json={"version": 1, "pinned": True})
    assert resp.status_code == 200 and resp.json()["pinned"] is True


# --- selection (SEM-21) ---------------------------------------------------------------


async def content_of(client: httpx.AsyncClient, prompt: str) -> str:
    resp = await client.post("/v1/chat/completions", json=chat(prompt))
    return resp.json()["choices"][0]["message"]["content"]  # type: ignore[no-any-return]


async def test_default_policy_always_returns_the_first(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record, "pick", "one")
    await record.put(
        f"/api/v1/entries/{entry['id']}", json={"version": 1, "add_response": {"content": "two"}}
    )
    assert [await content_of(record, "pick") for _ in range(3)] == ["one", "one", "one"]


async def test_sequence_returns_the_nth_then_sticks_on_the_last(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record, "seq", "one")
    await record.put(
        f"/api/v1/entries/{entry['id']}",
        json={"version": 1, "add_response": {"content": "two"}, "selection": "sequence"},
    )
    assert [await content_of(record, "seq") for _ in range(4)] == ["one", "two", "two", "two"]


async def test_cycle_wraps_around(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record, "cyc", "one")
    await record.put(
        f"/api/v1/entries/{entry['id']}",
        json={
            "version": 1,
            "add_response": {"content": "two"},
            "selection": "cycle",
        },
    )
    assert [await content_of(record, "cyc") for _ in range(5)] == [
        "one",
        "two",
        "one",
        "two",
        "one",
    ]


async def test_invalid_selection_is_rejected(record: httpx.AsyncClient) -> None:
    entry = await make_entry(record)
    resp = await record.put(
        f"/api/v1/entries/{entry['id']}", json={"version": 1, "selection": "random"}
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_selection"


async def test_streaming_uses_the_selected_response(record: httpx.AsyncClient) -> None:
    import json

    entry = await make_entry(record, "streamed seq", "one")
    await record.put(
        f"/api/v1/entries/{entry['id']}",
        json={"version": 1, "add_response": {"content": "two"}, "selection": "sequence"},
    )
    await content_of(record, "streamed seq")  # consumes "one"
    async with record.stream(
        "POST", "/v1/chat/completions", json=chat("streamed seq", stream=True)
    ) as resp:
        lines = [line async for line in resp.aiter_lines() if line]
    text = "".join(
        json.loads(line[6:])["choices"][0]["delta"].get("content") or "" for line in lines[:-1]
    )
    assert text == "two"
