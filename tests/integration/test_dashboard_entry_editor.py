"""Fixing a recording by hand on the entry page (SEM-50)."""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support.server import running_server
from tests.support.ui import Tab

pytestmark = pytest.mark.integration

TOOLS = [
    {
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
]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    with running_server(tmp_path, mode="replay") as url:
        yield url


def add(url: str, prompt: str, answer: Any = None, **request: Any) -> dict[str, Any]:
    resp = httpx.post(
        f"{url}/api/v1/projects/default/entries",
        json={
            "request": {"messages": [{"role": "user", "content": prompt}], **request},
            "response": answer or {"content": "original"},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def entry(url: str, entry_id: str) -> dict[str, Any]:
    return httpx.get(f"{url}/api/v1/entries/{entry_id}").json()  # type: ignore[no-any-return]


def answer_of(url: str, entry_id: str) -> str:
    return str(entry(url, entry_id)["responses"][0]["message"]["content"])


def open_entry(url: str, entry_id: str) -> Tab:
    tab = Tab(url, f"/projects/default/entries/{entry_id}")
    tab.wait_for(lambda m: "original" in str(m) or "get_weather" in str(m), 5)
    return tab


def editor(tab: Tab) -> dict[str, Any]:
    return tab.find("codeeditor", label="Edit answer")


def press(tab: Tab, label: str) -> None:
    tab.send(tab.find("button", label=label), "click")


def said(text: str) -> Any:
    return lambda m: m["type"] == "patch" and text in str(m)


def test_an_edit_is_saved_replays_and_bumps_the_version(server: str) -> None:
    stored = add(server, "fix me")
    tab = open_entry(server, stored["id"])
    try:
        mark = tab.mark()
        tab.send(editor(tab), "input", {"value": "fixed by hand"})
        press(tab, "Save answer")
        tab.wait_for(said("Saved."), 4, mark)
        now = entry(server, stored["id"])
        assert now["version"] == 2 and answer_of(server, stored["id"]) == "fixed by hand"
        history = httpx.get(f"{server}/api/v1/entries/{stored['id']}/history").json()["revisions"]
        assert history[0]["actor"] == "human"
        body = {"model": "m", "messages": [{"role": "user", "content": "fix me"}]}
        replay = httpx.post(server + "/v1/chat/completions", json=body).json()
        assert replay["choices"][0]["message"]["content"] == "fixed by hand"
    finally:
        tab.close()


def test_a_stale_edit_shows_a_conflict_and_keeps_the_other_persons_answer(server: str) -> None:
    stored = add(server, "contested")
    tab = open_entry(server, stored["id"])
    try:
        tab.send(editor(tab), "input", {"value": "mine"})  # a draft the viewer has started
        httpx.put(
            f"{server}/api/v1/entries/{stored['id']}",
            json={"version": 1, "response": {"content": "theirs"}},
        )
        time.sleep(0.5)  # their change reaches the page, which must not touch the draft
        mark = tab.mark()
        press(tab, "Save answer")
        tab.wait_for(said("Someone changed this entry"), 4, mark)
        assert answer_of(server, stored["id"]) == "theirs"
        press(tab, "Reload")
        tab.wait_for(said("theirs"), 4, mark)
    finally:
        tab.close()


def test_a_change_made_elsewhere_reaches_an_editor_nobody_has_typed_in(server: str) -> None:
    stored = add(server, "watched")
    tab = open_entry(server, stored["id"])
    try:
        mark = tab.mark()
        httpx.put(
            f"{server}/api/v1/entries/{stored['id']}",
            json={"version": 1, "response": {"content": "changed behind your back"}},
        )
        tab.wait_for(said("changed behind your back"), 4, mark)
    finally:
        tab.close()


def test_pinning_survives_a_save_and_the_pin_can_be_removed(server: str) -> None:
    stored = add(server, "pin me")
    tab = open_entry(server, stored["id"])
    try:
        pin = tab.find("button", label="Pin")  # the same button reads "Unpin" once pinned
        mark = tab.mark()
        tab.send(pin, "click")
        tab.wait_for(said("Pin changed."), 4, mark)
        assert entry(server, stored["id"])["pinned"] is True
        mark = tab.mark()
        tab.send(editor(tab), "input", {"value": "edited while pinned"})
        press(tab, "Save answer")
        tab.wait_for(said("Saved."), 4, mark)
        now = entry(server, stored["id"])
        assert now["pinned"] is True and answer_of(server, stored["id"]) == "edited while pinned"
        mark = tab.mark()
        tab.send(pin, "click")
        tab.wait_for(said("Pin changed."), 4, mark)
        assert entry(server, stored["id"])["pinned"] is False
    finally:
        tab.close()


def test_a_bad_tool_call_is_explained_and_nothing_is_saved(server: str) -> None:
    answer = {"message": {"content": None, "tool_calls": [
        {"function": {"name": "get_weather", "arguments": {"city": "KL"}}}]}}  # fmt: skip
    stored = add(server, "weather?", answer, tools=TOOLS)
    tab = open_entry(server, stored["id"])
    try:
        bad = json.dumps({"message": {"content": None, "tool_calls": [
            {"function": {"name": "nope", "arguments": {}}}]}})  # fmt: skip
        mark = tab.mark()
        tab.send(editor(tab), "input", {"value": bad})
        press(tab, "Save answer")
        tab.wait_for(said("Unknown tool"), 4, mark)
        assert entry(server, stored["id"])["version"] == 1
    finally:
        tab.close()


def test_a_tool_call_opens_as_json_and_saves_back(server: str) -> None:
    answer = {"message": {"content": None, "tool_calls": [
        {"function": {"name": "get_weather", "arguments": {"city": "KL"}}}]}}  # fmt: skip
    stored = add(server, "weather again", answer, tools=TOOLS)
    tab = open_entry(server, stored["id"])
    try:
        mark = tab.mark()
        changed = json.dumps({"message": {"content": None, "tool_calls": [
            {"function": {"name": "get_weather", "arguments": {"city": "Ipoh"}}}]}})  # fmt: skip
        tab.send(editor(tab), "input", {"value": changed})
        press(tab, "Save answer")
        tab.wait_for(said("Saved."), 4, mark)
        call = entry(server, stored["id"])["responses"][0]["message"]["tool_calls"][0]
        assert json.loads(call["function"]["arguments"]) == {"city": "Ipoh"}
    finally:
        tab.close()


def test_another_answer_can_be_added_and_the_list_edited(server: str) -> None:
    stored = add(server, "two answers")
    tab = open_entry(server, stored["id"])
    try:
        mark = tab.mark()
        tab.send(tab.find("codeeditor", label="Another answer"), "input", {"value": "the second"})
        press(tab, "Add answer")
        tab.wait_for(said("Answer added."), 4, mark)
        now = entry(server, stored["id"])
        assert len(now["responses"]) == 2 and now["selection"] == "sequence"
        mark = tab.mark()
        tab.send(editor(tab), "input", {"value": "not a list"})
        press(tab, "Save answer")
        tab.wait_for(said("not valid JSON"), 4, mark)
    finally:
        tab.close()


def test_deleting_removes_the_entry(server: str) -> None:
    stored = add(server, "delete me")
    tab = open_entry(server, stored["id"])
    try:
        press(tab, "Delete entry")
        for _ in range(50):
            if httpx.get(f"{server}/api/v1/entries/{stored['id']}").status_code == 404:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the entry was not deleted")
    finally:
        tab.close()
