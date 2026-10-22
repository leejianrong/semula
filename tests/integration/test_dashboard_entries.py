"""The Entries browser and the entry page, driven like a browser tab (SEM-49)."""

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
