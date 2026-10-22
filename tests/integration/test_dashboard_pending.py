"""The Pending screen, driven like a browser tab over its event stream (SEM-48)."""

import json
import threading
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
    with running_server(tmp_path, mode="record", record_timeout=20.0) as url:
        yield url


def tool_call(name: str, arguments: dict[str, Any]) -> str:
    call = {"function": {"name": name, "arguments": arguments}}
    return json.dumps({"message": {"content": None, "tool_calls": [call]}})


def park(url: str, prompt: str, **extra: Any) -> dict[str, Any]:
    """Make a call that parks in a thread; ``result`` fills in when it is released."""
    out: dict[str, Any] = {}

    def call() -> None:
        body = {"model": "m", "messages": [{"role": "user", "content": prompt}], **extra}
        out["response"] = httpx.post(url + "/v1/chat/completions", json=body, timeout=25)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    out["thread"] = thread
    return out


def pending_id(url: str) -> str:
    for _ in range(200):
        items = httpx.get(url + "/api/v1/pending").json()["pending"]
        if items:
            return str(items[0]["id"])
        time.sleep(0.02)
    raise AssertionError("never parked")


def rows_of(tab: Tab) -> list[dict[str, Any]]:
    return tab.find("table", label="Parked calls")["props"]["rows"]  # type: ignore[no-any-return]


def saw_rows(text: str) -> Any:
    return lambda m: m["type"] == "patch" and text in str(m["changes"])


def test_a_parked_call_appears_live_and_answering_releases_it(server: str) -> None:
    tab = Tab(server, "/projects/default/pending")
    try:
        started = time.perf_counter()
        held = park(server, "Where is my parcel?")
        tab.wait_for(saw_rows("Where is my parcel?"), 3)
        assert time.perf_counter() - started < 2
        key = pending_id(server)

        mark = tab.mark()
        tab.send(tab.find("table", label="Parked calls"), "select", {"key": key})
        tab.wait_for(lambda m: "Where is my parcel?" in str(m), 3, mark)  # the full request

        tab.send(tab.find("textarea", label="Answer"), "input", {"value": "On its way."})
        tab.send(tab.find("button", label="Send answer"), "click")
        held["thread"].join(10)
        assert held["response"].json()["choices"][0]["message"]["content"] == "On its way."
        tab.wait_for(lambda m: "Answered" in str(m), 3, mark)
        assert httpx.get(server + "/api/v1/pending").json()["pending"] == []
    finally:
        tab.close()


def test_a_json_answer_with_a_bad_tool_call_shows_the_error_and_keeps_the_call_held(
    server: str,
) -> None:
    tab = Tab(server, "/projects/default/pending")
    try:
        held = park(server, "Weather in KL?", tools=TOOLS)
        key = None
        tab.wait_for(saw_rows("Weather in KL?"), 3)
        key = pending_id(server)
        tab.send(tab.find("table", label="Parked calls"), "select", {"key": key})
        field = tab.find("textarea", label="Answer")
        send = tab.find("button", label="Send answer")

        bad = tool_call("nope", {})
        mark = tab.mark()
        tab.send(field, "input", {"value": bad})
        tab.send(send, "click")
        tab.wait_for(lambda m: "invalid_answer" in str(m) or "nope" in str(m), 3, mark)
        assert held["thread"].is_alive()
        assert httpx.get(server + "/api/v1/pending").json()["pending"][0]["id"] == key

        good = tool_call("get_weather", {"city": "KL"})
        tab.send(field, "input", {"value": good})
        tab.send(send, "click")
        held["thread"].join(10)
        message = held["response"].json()["choices"][0]["message"]
        assert message["tool_calls"][0]["function"]["name"] == "get_weather"
    finally:
        tab.close()


def test_broken_json_is_explained_without_calling_the_service(server: str) -> None:
    tab = Tab(server, "/projects/default/pending")
    try:
        held = park(server, "Broken json please")
        tab.wait_for(saw_rows("Broken json please"), 3)
        key = pending_id(server)
        tab.send(tab.find("table", label="Parked calls"), "select", {"key": key})
        mark = tab.mark()
        tab.send(tab.find("textarea", label="Answer"), "input", {"value": '{"content": '})
        tab.send(tab.find("button", label="Send answer"), "click")
        tab.wait_for(lambda m: "not valid" in str(m), 3, mark)
        assert held["thread"].is_alive()
        httpx.post(f"{server}/api/v1/pending/{key}/answer", json={"content": "done"})
        held["thread"].join(10)
    finally:
        tab.close()


def test_a_call_answered_elsewhere_leaves_the_list(server: str) -> None:
    tab = Tab(server, "/projects/default/pending")
    try:
        held = park(server, "Answered by an agent")
        tab.wait_for(saw_rows("Answered by an agent"), 3)
        key = pending_id(server)
        mark = tab.mark()
        httpx.post(f"{server}/api/v1/pending/{key}/answer", json={"content": "agent says hi"})
        tab.wait_for(
            lambda m: m["type"] == "patch" and '"rows": []' in str(m).replace("'", '"'), 3, mark
        )
        held["thread"].join(10)
    finally:
        tab.close()


def test_the_nav_counts_parked_calls_live(server: str) -> None:
    tab = Tab(server, "/projects/default/pending")
    try:
        badge = tab.find("badge")
        assert badge["props"]["text"] == "0"
        held = park(server, "Count me")
        tab.wait_for(
            lambda m: (
                m["type"] == "patch"
                and any(
                    c.get("target") == badge["id"] and c["props"].get("text") == "1"
                    for c in m["changes"]
                )
            ),
            4,
        )
        httpx.post(f"{server}/api/v1/pending/{pending_id(server)}/answer", json={"content": "x"})
        held["thread"].join(10)
    finally:
        tab.close()
