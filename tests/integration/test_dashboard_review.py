"""The review queue screen: compare a near miss, approve or reject it (SEM-51)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.test_lexical_match import BASE, OTHER_WEAK, WEAK, ask
from tests.support.server import running_server
from tests.support.ui import Tab

pytestmark = pytest.mark.integration


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    with running_server(tmp_path, mode="replay") as url:
        seeded = httpx.post(
            url + "/api/v1/projects/default/entries",
            json={"request": ask(BASE), "response": {"content": "the recorded answer"}},
        )
        assert seeded.status_code == 201
        yield url


def miss(url: str, text: str = WEAK) -> int:
    return httpx.post(url + "/v1/chat/completions", json=ask(text)).status_code


def candidates(url: str, status: str = "open") -> list[dict[str, Any]]:
    return httpx.get(url + "/api/v1/candidates", params={"status": status}).json()["candidates"]  # type: ignore[no-any-return]


def queue(tab: Tab) -> dict[str, Any]:
    return tab.find("table", label="Review queue")


def listed() -> Any:
    return lambda m: (
        m["type"] == "patch"
        and any(c.get("props", {}).get("rows") for c in m["changes"] if c.get("props"))
    )


def said(text: str) -> Any:
    return lambda m: m["type"] == "patch" and text in str(m)


def test_a_near_miss_appears_in_the_queue_live(server: str) -> None:
    tab = Tab(server, "/projects/default/review")
    try:
        assert miss(server) == 424
        tab.wait_for(listed(), 5)
    finally:
        tab.close()


def test_selecting_one_shows_both_prompts_and_the_recorded_answer(server: str) -> None:
    miss(server)
    (found,) = candidates(server)
    tab = Tab(server, "/projects/default/review")
    try:
        tab.wait_for(listed(), 5)
        mark = tab.mark()
        tab.send(queue(tab), "select", {"key": found["id"]})
        tab.wait_for(said("the recorded answer"), 5, mark)
        tab.wait_for(said("def mul(x, y)"), 5, mark)  # the diff: the new prompt ...
        tab.wait_for(said("def add(a, b)"), 5, mark)  # ... against the recorded one
    finally:
        tab.close()


def test_approving_makes_the_prompt_hit_and_leaves_the_queue(server: str) -> None:
    miss(server)
    (found,) = candidates(server)
    tab = Tab(server, "/projects/default/review")
    try:
        tab.wait_for(listed(), 5)
        tab.send(queue(tab), "select", {"key": found["id"]})
        tab.wait_for(said("the recorded answer"), 5)
        mark = tab.mark()
        tab.send(tab.find("button", label="Approve"), "click")
        tab.wait_for(said("now served from the entry"), 5, mark)
        assert candidates(server) == []
        hit = httpx.post(server + "/v1/chat/completions", json=ask(WEAK))
        assert hit.status_code == 200
        assert hit.json()["choices"][0]["message"]["content"] == "the recorded answer"
    finally:
        tab.close()


def press(tab: Tab, keys: str) -> None:
    node = next(
        n
        for n in tab.nodes()
        if n["type"] == "shortcuts" and any(k["keys"] == keys for k in n["props"]["keys"])
    )
    tab.send(node, "press", {"keys": keys})


def test_a_and_r_decide_the_selected_near_miss_and_do_nothing_with_none(server: str) -> None:
    miss(server)
    (found,) = candidates(server)
    tab = Tab(server, "/projects/default/review")
    try:
        tab.wait_for(listed(), 5)
        press(tab, "a")  # nothing selected: the buttons are disabled, so is the key
        press(tab, "r")
        assert len(candidates(server)) == 1
        tab.send(queue(tab), "select", {"key": found["id"]})
        tab.wait_for(said("the recorded answer"), 5)
        mark = tab.mark()
        press(tab, "r")
        tab.wait_for(said("will not come back"), 5, mark)
        assert [c["status"] for c in candidates(server, "rejected")] == ["rejected"]
        miss(server, OTHER_WEAK)
        (second,) = candidates(server)
        tab.send(queue(tab), "select", {"key": second["id"]})
        tab.wait_for(said("the recorded answer"), 5, mark)
        mark = tab.mark()
        press(tab, "a")
        tab.wait_for(said("now served from the entry"), 5, mark)
    finally:
        tab.close()


def test_rejecting_removes_it_and_it_stays_a_miss(server: str) -> None:
    miss(server)
    (found,) = candidates(server)
    tab = Tab(server, "/projects/default/review")
    try:
        tab.wait_for(listed(), 5)
        tab.send(queue(tab), "select", {"key": found["id"]})
        tab.wait_for(said("the recorded answer"), 5)
        mark = tab.mark()
        tab.send(tab.find("button", label="Reject"), "click")
        tab.wait_for(said("will not come back"), 5, mark)
        assert candidates(server) == []
        assert [c["status"] for c in candidates(server, "rejected")] == ["rejected"]
        assert miss(server) == 424
        assert candidates(server) == []  # a rejected pairing is never queued again
    finally:
        tab.close()


def test_the_nav_counts_the_queue_and_a_decision_elsewhere_clears_the_row(server: str) -> None:
    tab = Tab(server, "/projects/default/review")
    try:
        miss(server)
        miss(server, OTHER_WEAK)
        review_item = next(
            n["id"]
            for n in tab.nodes()
            if n["type"] == "navitem" and n["props"]["label"] == "Review"
        )
        tab.wait_for(
            lambda m: (
                m["type"] == "patch"
                and any(
                    c.get("target") == review_item and c["props"].get("badge") == "2"
                    for c in m["changes"]
                )
            ),
            5,
        )
        first = candidates(server)[0]
        mark = tab.mark()
        httpx.post(f"{server}/api/v1/candidates/{first['id']}/reject")
        tab.wait_for(listed(), 5, mark)
    finally:
        tab.close()
