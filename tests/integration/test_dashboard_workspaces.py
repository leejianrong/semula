"""A person who belongs to two workspaces picks the one to work in (SEM-92)."""

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration.test_dashboard_hosted import (
    COOKIE,
    PROVIDER,
    SECRET,
    World,
    open_as,
)
from tests.support.server import running_server

pytestmark = pytest.mark.integration


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    from semula.scope import Scope

    invite = w.service.for_scope(Scope("rival")).create_invitation("ada@example.com", "viewer")
    w.service.accept_invitation(invite["token"], w.people["ada"])  # Ada also belongs to rival
    return w


@pytest.fixture
def server(world: World, tmp_path: Path) -> Iterator[str]:
    with running_server(
        tmp_path,
        hosted=True,
        secret=SECRET,
        auth_providers={"google": PROVIDER},
        database_url=world.db_url,
        mode="replay",
    ) as url:
        yield url


def wait(check, what: str, seconds: float = 6.0):  # type: ignore[no-untyped-def]
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if found := check():
            return found
        time.sleep(0.05)
    raise AssertionError(f"never saw: {what}")


def active(world: World, who: str) -> str | None:
    session = world.service.authenticate_session(world.sessions[who][COOKIE])
    return None if session is None else session["active_workspace"]


def test_a_person_in_one_workspace_is_offered_no_switcher(server: str, world: World) -> None:
    tab = open_as(server, world, "edd")
    try:
        tab.wait_for(lambda m: "held for a person" in str(m), 5)
        with pytest.raises(LookupError):
            tab.find("select", label="Workspace")
    finally:
        tab.close()


def test_picking_another_workspace_moves_the_sign_in_and_the_next_page_load_follows(
    server: str, world: World
) -> None:
    tab = open_as(server, world, "ada")
    try:
        tab.wait_for(lambda m: "held for a person" in str(m), 5)
        mark = tab.mark()
        tab.send(tab.find("select", label="Workspace"), "change", {"value": "rival"})
        wait(lambda: active(world, "ada") == "rival", "the switch")
        wait(lambda: any(m["type"] == "signed_out" for m in tab.messages[mark:]), "tab ended")
        assert "Reload this page" in tab.since(mark)
    finally:
        tab.close()
    fresh = open_as(server, world, "ada")  # what a reload does
    try:
        fresh.wait_for(lambda m: "rival secret" in str(m), 5)
        text = json.dumps(fresh.messages)
        assert "held for a person" not in text  # acme's call is no longer in view
        assert "viewer in rival" in text  # and the role is the one held there
        with pytest.raises(LookupError):
            fresh.find("button", label="Send answer")  # a viewer in rival, though admin in acme
    finally:
        fresh.close()


def test_a_workspace_the_person_does_not_belong_to_is_refused(server: str, world: World) -> None:
    tab = open_as(server, world, "ada")
    try:
        tab.wait_for(lambda m: "held for a person" in str(m), 5)
        mark = tab.mark()
        tab.send(tab.find("select", label="Workspace"), "change", {"value": "elsewhere"})
        wait(lambda: "do not belong" in tab.since(mark), "the refusal")
        assert active(world, "ada") != "elsewhere"
        assert not any(m["type"] == "signed_out" for m in tab.messages[mark:])
    finally:
        tab.close()
