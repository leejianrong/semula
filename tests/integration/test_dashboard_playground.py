"""The playground: ask what would happen to a prompt (SEM-52)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support.server import running_server
from tests.support.ui import Tab

pytestmark = pytest.mark.integration


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    with running_server(tmp_path, mode="replay") as url:
        httpx.post(
            url + "/api/v1/projects/default/entries",
            json={
                "request": {"messages": [{"role": "user", "content": "Name a fruit."}]},
                "response": {"content": "Mango"},
            },
        )
        yield url


def said(text: str) -> Any:
    return lambda m: m["type"] == "patch" and text in str(m)


def ask(tab: Tab, prompt: str, system: str = "") -> int:
    mark = tab.mark()
    tab.send(tab.find("codeeditor", label="Test prompt"), "input", {"value": prompt})
    if system:
        tab.send(
            tab.find("textinput", label="Test system prompt (optional)"), "input", {"value": system}
        )
    tab.send(tab.find("button", label="Test it"), "click")
    return mark


def test_a_recorded_prompt_shows_the_tier_and_the_answer(server: str) -> None:
    tab = Tab(server, "/projects/default/playground")
    try:
        mark = ask(tab, "Name a fruit.")
        tab.wait_for(said("Would serve the recorded answer (Exact)."), 5, mark)
        tab.wait_for(said("Mango"), 5, mark)
        tab.wait_for(said("Match"), 5, mark)  # the rail: the exact tier answered
    finally:
        tab.close()


def test_a_near_miss_says_what_was_nearest_and_that_it_would_go_to_review(server: str) -> None:
    tab = Tab(server, "/projects/default/playground")
    try:
        mark = ask(tab, "Name a fruit please.")
        tab.wait_for(said("Would fail with 424"), 5, mark)
        tab.wait_for(said('Nearest recorded prompt: "Name a fruit."'), 5, mark)
    finally:
        tab.close()


def test_nothing_is_logged_or_counted_by_asking(server: str) -> None:
    tab = Tab(server, "/projects/default/playground")
    try:
        mark = ask(tab, "Name a fruit.")
        tab.wait_for(said("Would serve"), 5, mark)
        assert httpx.get(server + "/api/v1/log").json()["calls"] == []
        entry = httpx.get(server + "/api/v1/projects/default/entries").json()["entries"][0]
        assert entry["hit_count"] == 0
    finally:
        tab.close()


def test_a_recording_project_says_it_would_park(server: str) -> None:
    httpx.put(server + "/api/v1/mode", json={"mode": "record", "project": "default"})
    tab = Tab(server, "/projects/default/playground")
    try:
        mark = ask(tab, "something nobody recorded")
        tab.wait_for(said("Would park this call"), 5, mark)
    finally:
        tab.close()


def test_a_missing_prompt_and_bad_tools_are_explained(server: str) -> None:
    tab = Tab(server, "/projects/default/playground")
    try:
        mark = tab.mark()
        tab.send(tab.find("button", label="Test it"), "click")
        tab.wait_for(said("Write the prompt"), 5, mark)
        tab.send(tab.find("codeeditor", label="Test prompt"), "input", {"value": "x"})
        tab.send(tab.find("codeeditor", label="Test tools (optional)"), "input", {"value": "{"})
        mark = tab.mark()
        tab.send(tab.find("button", label="Test it"), "click")
        tab.wait_for(said("not valid JSON"), 5, mark)
    finally:
        tab.close()
