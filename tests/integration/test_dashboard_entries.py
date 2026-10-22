"""The Entries browser and the entry page, driven like a browser tab (SEM-49)."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support.server import running_server
from tests.support.ui import Tab

pytestmark = pytest.mark.integration


def add(url: str, prompt: str, answer: str = "ok", project: str = "default") -> dict[str, Any]:
    resp = httpx.post(
        f"{url}/api/v1/projects/{project}/entries",
        json={
            "request": {"messages": [{"role": "user", "content": prompt}]},
            "response": {"content": answer},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    with running_server(tmp_path, mode="replay") as url:
        yield url


def table_label(tab: Tab) -> dict[str, Any]:
    return tab.find("table", label="Entries")


def has_rows(*prompts: str) -> Any:
    def found(m: dict[str, Any]) -> bool:
        rows = [
            r
            for c in m.get("changes", [])
            if c.get("props", {}).get("rows") is not None
            for r in c["props"]["rows"]
        ]
        return m["type"] == "patch" and sorted(r["prompt"] for r in rows) == sorted(prompts)

    return found


def test_the_list_shows_the_entries_and_follows_new_ones_live(server: str) -> None:
    add(server, "first recorded prompt")
    tab = Tab(server, "/projects/default/entries")
    try:
        tab.wait_for(has_rows("first recorded prompt"), 4)
        add(server, "recorded while the tab is open")
        tab.wait_for(has_rows("first recorded prompt", "recorded while the tab is open"), 4)
    finally:
        tab.close()


def test_searching_by_text_finds_an_entry(server: str) -> None:
    add(server, "refund policy for damaged goods")
    add(server, "opening hours on Sunday")
    tab = Tab(server, "/projects/default/entries")
    try:
        tab.wait_for(has_rows("refund policy for damaged goods", "opening hours on Sunday"), 4)
        mark = tab.mark()
        tab.send(tab.find("textinput", label="Search"), "input", {"value": "refund"})
        tab.wait_for(has_rows("refund policy for damaged goods"), 4, mark)
    finally:
        tab.close()


def test_the_filter_and_sort_menus_change_the_list(server: str) -> None:
    pinned = add(server, "a pinned prompt")
    add(server, "an ordinary prompt")
    httpx.put(
        f"{server}/api/v1/entries/{pinned['id']}",
        json={"version": 1, "pinned": True},
        headers={"X-Semula-Actor": "human"},
    )
    tab = Tab(server, "/projects/default/entries")
    try:
        tab.wait_for(has_rows("a pinned prompt", "an ordinary prompt"), 4)
        mark = tab.mark()
        tab.send(tab.find("select", label="Filter"), "change", {"value": "Pinned"})
        message, _ = tab.wait_for(has_rows("a pinned prompt"), 4, mark)
        row = next(r for c in message["changes"] for r in c.get("props", {}).get("rows", []))
        assert row["flags"] == "Pinned" and row["version"] == "v2"
    finally:
        tab.close()


def test_selecting_a_row_opens_the_entry_at_its_own_address(server: str) -> None:
    entry = add(server, "open me", answer="the stored answer")
    tab = Tab(server, "/projects/default/entries")
    try:
        tab.wait_for(has_rows("open me"), 4)
        mark = tab.mark()
        tab.send(table_label(tab), "select", {"key": entry["id"]})
        tab.wait_for(lambda m: "the stored answer" in str(m), 4, mark)
    finally:
        tab.close()


def test_a_deep_link_opens_an_entry_with_its_request_answer_and_history(server: str) -> None:
    entry = add(server, "deep linked prompt", answer="deep linked answer")
    tab = Tab(server, f"/projects/default/entries/{entry['id']}")
    try:
        tab.wait_for(lambda m: "deep linked answer" in str(m), 4)
        tab.wait_for(lambda m: "deep linked prompt" in str(m), 4)
        tab.wait_for(lambda m: "created" in str(m), 4)  # the history line
    finally:
        tab.close()


def test_an_entry_that_does_not_exist_says_so(server: str) -> None:
    tab = Tab(server, "/projects/default/entries/01J0000000000000000000000")
    try:
        tab.wait_for(lambda m: "There is no such entry" in str(m), 4)
    finally:
        tab.close()


def fill(tab: Tab, label: str, value: str) -> None:
    tab.send(tab.find("codeeditor", label=label), "input", {"value": value})


def test_a_new_entry_can_be_written_in_the_dashboard_and_then_replays(server: str) -> None:
    tab = Tab(server, "/projects/default/entries")
    try:
        mark = tab.mark()
        tab.send(tab.find("button", label="New entry"), "click")
        fill(tab, "Prompt", "What is your refund window?")
        fill(tab, "New answer", "Fourteen days.")
        tab.send(tab.find("button", label="Create entry"), "click")
        tab.wait_for(lambda m: "Fourteen days." in str(m), 5, mark)
        listed = httpx.get(f"{server}/api/v1/projects/default/entries").json()["entries"]
        assert [e["last_message"] for e in listed] == ["What is your refund window?"]
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "What is your refund window?"}],
        }
        reply = httpx.post(server + "/v1/chat/completions", json=body).json()
        assert reply["choices"][0]["message"]["content"] == "Fourteen days."
    finally:
        tab.close()


def test_a_new_entry_can_be_pinned_and_carry_a_system_prompt_and_tools(server: str) -> None:
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    tab = Tab(server, "/projects/default/entries")
    try:
        tab.send(tab.find("button", label="New entry"), "click")
        fill(tab, "Prompt", "Call f")
        tab.send(
            tab.find("textinput", label="System prompt (optional)"), "input", {"value": "Be terse"}
        )
        fill(tab, "Tools (optional)", json.dumps(tools))
        call = {"function": {"name": "f", "arguments": {}}}
        fill(tab, "New answer", json.dumps({"message": {"content": None, "tool_calls": [call]}}))
        tab.send(
            tab.find("switch", label="Pin it, so agents cannot overwrite it"),
            "change",
            {"value": True},
        )
        mark = tab.mark()
        tab.send(tab.find("button", label="Create entry"), "click")
        tab.wait_for(lambda m: "pinned" in str(m), 5, mark)
        made = httpx.get(f"{server}/api/v1/projects/default/entries").json()["entries"][0]
        assert made["pinned"] is True and made["has_tools"] is True
        full = httpx.get(f"{server}/api/v1/entries/{made['id']}").json()
        assert [m["role"] for m in full["request"]["messages"]] == ["system", "user"]
    finally:
        tab.close()


def test_a_bad_answer_is_explained_in_the_form_and_nothing_is_created(server: str) -> None:
    tab = Tab(server, "/projects/default/entries")
    try:
        tab.send(tab.find("button", label="New entry"), "click")
        fill(tab, "Prompt", "Bad answer please")
        fill(
            tab,
            "New answer",
            '{"message": {"content": null, "tool_calls": [{"function": {"name": "x"}}]}}',
        )
        mark = tab.mark()
        tab.send(tab.find("button", label="Create entry"), "click")
        tab.wait_for(lambda m: m["type"] == "patch" and "invalid_answer: " in str(m), 5, mark)
        assert httpx.get(f"{server}/api/v1/projects/default/entries").json()["total"] == 0
        fill(tab, "New answer", "")
        mark = tab.mark()
        tab.send(tab.find("button", label="Create entry"), "click")
        tab.wait_for(lambda m: "Write an answer first" in str(m), 5, mark)
    finally:
        tab.close()


def test_an_existing_prompt_is_not_overwritten_from_the_form(server: str) -> None:
    add(server, "already recorded", answer="the old answer")
    tab = Tab(server, "/projects/default/entries")
    try:
        tab.send(tab.find("button", label="New entry"), "click")
        fill(tab, "Prompt", "already recorded")
        fill(tab, "New answer", "a different answer")
        mark = tab.mark()
        tab.send(tab.find("button", label="Create entry"), "click")
        tab.wait_for(lambda m: "already exists" in str(m), 5, mark)
        listed = httpx.get(f"{server}/api/v1/projects/default/entries").json()["entries"]
        assert len(listed) == 1
        only = httpx.get(f"{server}/api/v1/entries/{listed[0]['id']}").json()
        assert only["responses"][0]["message"]["content"] == "the old answer"
    finally:
        tab.close()
