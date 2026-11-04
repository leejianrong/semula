"""One clear action per view and honest empty states (SEM-133).

A button that needs a selection is disabled until there is one; the second action of a view is
not drawn like the first; the entry editor shows an answer once; the playground offers the
entry only when it found one.
"""

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.test_lexical_match import BASE, WEAK, ask
from tests.support.server import running_server
from tests.support.ui import Tab

pytestmark = pytest.mark.integration


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    with running_server(tmp_path, mode="replay", record_timeout=20.0) as url:
        made = httpx.post(
            url + "/api/v1/projects/default/entries",
            json={"request": ask(BASE), "response": {"content": "the recorded answer"}},
        )
        assert made.status_code == 201
        yield url


def disabled(tab: Tab, label: str) -> bool:
    return bool(tab.find("button", label=label)["props"].get("disabled"))


def becomes(tab: Tab, button: dict[str, Any], flag: bool, after: int) -> None:
    tab.wait_for(
        lambda m: (
            m["type"] == "patch"
            and any(
                c.get("target") == button["id"] and c.get("props", {}).get("disabled") is flag
                for c in m["changes"]
            )
        ),
        5,
        after,
    )


def test_send_answer_is_disabled_until_a_parked_call_is_selected(server: str) -> None:
    httpx.put(server + "/api/v1/mode", json={"mode": "record"})
    tab = Tab(server, "/projects/default/pending")
    try:
        send = tab.find("button", label="Send answer")
        assert disabled(tab, "Send answer")
        threading.Thread(
            target=lambda: httpx.post(
                server + "/v1/chat/completions", json=ask("held"), timeout=25
            ),
            daemon=True,
        ).start()
        key = None
        for _ in range(200):
            held = httpx.get(server + "/api/v1/pending").json()["pending"]
            if held:
                key = held[0]["id"]
                break
            time.sleep(0.02)
        assert key
        tab.wait_for(lambda m: m["type"] == "patch" and "held" in str(m), 5)
        mark = tab.mark()
        tab.send(tab.find("table", label="Parked calls"), "select", {"key": key})
        becomes(tab, send, False, mark)
        mark = tab.mark()
        tab.send(tab.find("table", label="Parked calls"), "select", {"key": None})
        becomes(tab, send, True, mark)
        httpx.post(f"{server}/api/v1/pending/{key}/answer", json={"content": "ok"})
    finally:
        tab.close()


def test_approve_and_reject_are_disabled_until_a_near_miss_is_selected(server: str) -> None:
    assert httpx.post(server + "/v1/chat/completions", json=ask(WEAK)).status_code == 424
    tab = Tab(server, "/projects/default/review")
    try:
        approve = tab.find("button", label="Approve")
        assert disabled(tab, "Approve") and disabled(tab, "Reject")
        # the one filled button is Approve; Reject is set apart from it
        assert "variant" not in approve["props"]
        assert tab.find("button", label="Reject")["props"]["variant"] == "ghost"
        key = httpx.get(server + "/api/v1/candidates", params={"status": "open"}).json()[
            "candidates"
        ][0]["id"]
        mark = tab.mark()
        tab.send(tab.find("table", label="Review queue"), "select", {"key": key})
        becomes(tab, approve, False, mark)
    finally:
        tab.close()


def test_the_entry_editor_shows_the_answer_once_and_goes_back_with_a_link(server: str) -> None:
    made = httpx.get(server + "/api/v1/projects/default/entries").json()["entries"][0]
    tab = Tab(server, f"/projects/default/entries/{made['id']}")
    try:
        tab.wait_for(lambda m: "the recorded answer" in str(m), 5)
        titles = [n["props"].get("title") for n in tab.nodes() if n["type"] == "card"]
        assert "Answers" not in titles and "Edit the answer" in titles
        assert [n for n in tab.nodes() if n["type"] == "link" and "Back" in str(n["props"])]
        assert not [
            n for n in tab.nodes() if n["type"] == "button" and "Back" in n["props"]["label"]
        ]
        # Save is the filled button; Delete is not drawn like it
        assert "variant" not in tab.find("button", label="Save answer")["props"]
        assert tab.find("button", label="Delete entry")["props"]["variant"] == "ghost"
    finally:
        tab.close()


def test_the_playground_offers_the_entry_only_when_a_test_found_one(server: str) -> None:
    tab = Tab(server, "/projects/default/playground")
    try:
        opener = tab.find("button", label="Open the entry")
        assert disabled(tab, "Open the entry")
        mark = tab.mark()
        tab.send(tab.find("codeeditor", label="Test prompt"), "input", {"value": BASE})
        tab.send(tab.find("button", label="Test it"), "click")
        becomes(tab, opener, False, mark)
    finally:
        tab.close()
