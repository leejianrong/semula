"""The live log screen, driven like a browser tab over its event stream (SEM-47)."""

import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.support.server import running_server
from tests.support.ui import Tab

pytestmark = pytest.mark.integration

SYSTEM = "You are a support triage bot. Reply tersely."


def ask(prompt: str) -> dict[str, Any]:
    return {
        "model": "m",
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with running_server(tmp_path_factory.mktemp("ui"), mode="replay") as url:
        httpx.post(url + "/api/v1/projects", json={"id": "other"})
        for prompt in ("seeded hit", "other project hit"):
            project = "default" if prompt == "seeded hit" else "other"
            httpx.post(
                f"{url}/api/v1/projects/{project}/entries",
                json={"request": ask(prompt), "response": {"content": f"the answer to {prompt}"}},
            )
        httpx.post(url + "/v1/chat/completions", json=ask("seeded hit"))
        yield url


@pytest.fixture
def tab(server: str) -> Iterator[Tab]:
    opened = Tab(server)
    yield opened
    opened.close()


def table_rows(tab: Tab) -> list[dict[str, Any]]:
    return tab.find("table", label="Calls")["props"]["rows"]  # type: ignore[no-any-return]


def appended(text: str) -> Any:
    return lambda m: m["type"] == "patch" and text in str(m["changes"]) and "append" in str(m)


def test_a_new_call_reaches_an_open_tab_within_a_second(server: str, tab: Tab) -> None:
    started = time.perf_counter()
    httpx.post(server + "/v1/chat/completions", json=ask("a brand new question"))
    message, _ = tab.wait_for(appended("a brand new question"), 3)
    assert time.perf_counter() - started < 1
    (change,) = [c for c in message["changes"] if "append" in c]
    (row,) = change["append"]["rows"]
    assert row["result"] == "Miss" and row["project"] == "default" and row["when"]


def test_every_open_tab_gets_it(server: str, tab: Tab) -> None:
    other = Tab(server)
    try:
        httpx.post(server + "/v1/chat/completions", json=ask("shared by two tabs"))
        tab.wait_for(appended("shared by two tabs"), 3)
        other.wait_for(appended("shared by two tabs"), 3)
    finally:
        other.close()


def test_a_tab_opened_later_starts_with_the_recent_calls(server: str) -> None:
    httpx.post(server + "/v1/chat/completions", json=ask("asked before the tab opened"))
    late = Tab(server)
    try:
        assert "asked before the tab opened" in [r["prompt"] for r in table_rows(late)]
        assert "seeded hit" in [r["prompt"] for r in table_rows(late)]
    finally:
        late.close()


def test_another_projects_calls_stay_off_this_log(server: str, tab: Tab) -> None:
    mark = tab.mark()
    httpx.post(server + "/v1/projects/other/chat/completions", json=ask("only in other"))
    httpx.post(server + "/v1/chat/completions", json=ask("in default"))
    tab.wait_for(appended("in default"), 3)
    assert "only in other" not in tab.since(mark)
    assert "only in other" not in [r["prompt"] for r in table_rows(tab)]


def test_the_stats_follow_the_calls(server: str, tab: Tab) -> None:
    rate = tab.find("stat", label="Hit rate")
    median = tab.find("stat", label="Median")
    httpx.post(server + "/v1/chat/completions", json=ask("seeded hit"))
    tab.wait_for(
        lambda m: (
            m["type"] == "patch"
            and any(c["target"] == rate["id"] and c["props"]["value"] != "-" for c in m["changes"])
        ),
        4,
    )
    tab.wait_for(
        lambda m: (
            m["type"] == "patch"
            and any(
                c["target"] == median["id"] and c["props"]["value"].endswith("ms")
                for c in m["changes"]
            )
        ),
        4,
    )


def test_selecting_a_hit_shows_its_request_and_answer_and_the_rail(server: str, tab: Tab) -> None:
    row = next(r for r in table_rows(tab) if r["prompt"] == "seeded hit" and r["outcome"] == "hit")
    mark = tab.mark()
    tab.send(tab.find("table", label="Calls"), "select", {"key": row["id"]})
    tab.wait_for(lambda m: "the answer to seeded hit" in str(m), 3, mark)
    tab.wait_for(lambda m: "Skipped" in str(m), 3, mark)  # the rail is a separate patch
    shown = tab.since(mark)
    assert SYSTEM in shown and "Match" in shown and "Skipped" in shown and "Exact" in shown


def test_selecting_a_miss_says_only_the_preview_was_kept(server: str, tab: Tab) -> None:
    httpx.post(server + "/v1/chat/completions", json=ask("a miss to open"))
    tab.wait_for(appended("a miss to open"), 3)
    key = next(
        c["append"]["rows"][0]["id"]
        for m in tab.messages
        if m["type"] == "patch"
        for c in m["changes"]
        if "append" in c and c["append"]["rows"][0]["prompt"] == "a miss to open"
    )
    mark = tab.mark()
    tab.send(tab.find("table", label="Calls"), "select", {"key": key})
    tab.wait_for(lambda m: "Only the preview was kept" in str(m), 3, mark)
    tab.wait_for(lambda m: "a miss to open" in str(m), 3, mark)  # the message list, another patch


def test_the_show_menu_filters_the_table(server: str, tab: Tab) -> None:
    httpx.post(server + "/v1/chat/completions", json=ask("seeded hit"))
    httpx.post(server + "/v1/chat/completions", json=ask("will miss for the filter"))
    tab.wait_for(appended("will miss for the filter"), 3)
    table = tab.find("table", label="Calls")
    mark = tab.mark()
    tab.send(tab.find("select", label="Show"), "change", {"value": "Hits"})
    message, _ = tab.wait_for(
        lambda m: (
            m["type"] == "patch"
            and any(
                c["target"] == table["id"] and "rows" in c.get("props", {}) for c in m["changes"]
            )
        ),
        3,
    )
    rows = next(c["props"]["rows"] for c in message["changes"] if c["target"] == table["id"])
    assert rows and {r["outcome"] for r in rows} == {"hit"}
    # and new misses no longer appear while it is on
    mark = tab.mark()
    httpx.post(server + "/v1/chat/completions", json=ask("a miss while filtered"))
    time.sleep(0.6)
    assert "a miss while filtered" not in tab.changes_to(table, mark)


def test_pausing_stops_the_table_and_resuming_catches_up(server: str, tab: Tab) -> None:
    live = tab.find("switch", label="Live")
    tab.send(live, "change", {"value": False})
    time.sleep(0.3)
    mark = tab.mark()
    httpx.post(server + "/v1/chat/completions", json=ask("while paused"))
    time.sleep(0.8)
    calls = tab.find("table", label="Calls")
    assert "while paused" not in tab.changes_to(calls, mark)
    tab.send(live, "change", {"value": True})
    tab.wait_for(
        lambda m: (
            m["type"] == "patch"
            and any(
                c.get("target") == calls["id"] and "while paused" in str(c) for c in m["changes"]
            )
        ),
        3,
        mark,
    )


def test_choosing_another_project_moves_the_tab_there(server: str, tab: Tab) -> None:
    tab.send(tab.find("select", label="Project"), "change", {"value": "other"})
    message, _ = tab.wait_for(lambda m: m["type"] == "navigate", 3)
    assert message["path"] == "/projects/other/log"


def test_the_record_switch_sets_the_projects_mode_and_every_tab_follows(
    server: str, tab: Tab
) -> None:
    second = Tab(server)
    try:
        flag = second.find("switch", label="Record")

        def checked(value: bool) -> Any:
            return lambda m: (
                m["type"] == "patch"
                and any(
                    c["target"] == flag["id"] and c["props"].get("checked") is value
                    for c in m["changes"]
                )
            )

        tab.send(tab.find("switch", label="Record"), "change", {"value": True})
        second.wait_for(checked(True), 3)
        project = httpx.get(server + "/api/v1/projects/default").json()
        assert project["mode"] == "record" and project["settings"]["mode"] == "record"  # kept
        mark = second.mark()
        httpx.patch(
            server + "/api/v1/projects/default", json={"settings": {"mode": "replay"}}
        )  # changed from outside the dashboard
        second.wait_for(checked(False), 3, mark)
    finally:
        httpx.patch(server + "/api/v1/projects/default", json={"settings": {"mode": None}})
        second.close()


def test_the_switch_follows_the_projects_not_the_other_ones(server: str, tab: Tab) -> None:
    flag = tab.find("switch", label="Record")
    mark = tab.mark()
    httpx.patch(server + "/api/v1/projects/other", json={"settings": {"mode": "record"}})
    try:
        time.sleep(1.5)
        assert f'"target": "{flag["id"]}"' not in tab.since(mark)  # another project's mode
    finally:
        httpx.patch(server + "/api/v1/projects/other", json={"settings": {"mode": None}})


def test_a_parked_call_shows_in_the_parked_count(server: str, tab: Tab) -> None:
    parked = tab.find("stat", label="Parked")

    def count(value: str) -> Any:
        return lambda m: (
            m["type"] == "patch"
            and any(
                c["target"] == parked["id"] and c["props"]["value"] == value for c in m["changes"]
            )
        )

    httpx.put(server + "/api/v1/mode", json={"mode": "record"})
    try:
        threading.Thread(
            target=lambda: httpx.post(
                server + "/v1/chat/completions", json=ask("parked now"), timeout=30
            ),
            daemon=True,
        ).start()
        tab.wait_for(count("1"), 4)
        pending = httpx.get(server + "/api/v1/pending").json()["pending"][0]["id"]
        httpx.post(f"{server}/api/v1/pending/{pending}/answer", json={"content": "ok"})
        tab.wait_for(count("0"), 4)
    finally:
        httpx.put(server + "/api/v1/mode", json={"mode": "replay"})
