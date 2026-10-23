"""The admin screens of the hosted dashboard: members, tokens, audit, usage (SEM-91).

Each is driven as a browser tab would, and the effect is read back from the service."""

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from semula.db import Database
from semula.errors import SemulaError
from semula.oidc import Identity
from semula.service import Service
from tests.integration.test_dashboard_hosted import (
    COOKIE,
    PROVIDER,
    SECRET,
    World,
    open_as,
)
from tests.support.server import running_server
from tests.support.ui import Tab

pytestmark = pytest.mark.integration


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


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


def verify(world: World, secret: str) -> Any:
    """Who a token is, asked of a service with no cache of its own: the truth in the database,
    not what this process remembers (another process takes up to 30 s to notice a revoke)."""
    return Service(Database(world.db_url), world.service.settings).authenticate_token(secret)


def until(check: Callable[[], Any], what: str, seconds: float = 6.0) -> Any:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        found = check()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError(f"never saw: {what}")


def admin_on(server: str, world: World, page: str, who: str = "ada") -> Tab:
    tab = open_as(server, world, who, f"/workspace/{page}")
    return tab


def act(tab: Tab, table: str, action: str, key: str) -> None:
    """Press a row button: the table's actions are named a0, a1, ... in the order given."""
    tab.send(tab.find("table", label=table), "action", {"action": action, "key": key})


def type_into(tab: Tab, kind: str, label: str, value: str) -> None:
    tab.send(tab.find(kind, label=label), "input" if kind == "textinput" else "change",
             {"value": value})  # fmt: skip


def revealed(tab: Tab, mark: int, label: str) -> str:
    """The secret a SecretReveal was sent: it travels in a patch to this tab only."""
    hits = [
        c["props"]["value"]
        for m in tab.messages[mark:]
        if m["type"] == "patch"
        for c in m["changes"]
        if c.get("target") == tab.find("secretreveal", label=label)["id"]
        and isinstance(c.get("props", {}).get("value"), str)
        and c["props"]["value"]
    ]
    return hits[-1] if hits else ""


def error_shown(tab: Tab, mark: int, code: str) -> bool:
    return code in tab.since(mark)


# --- who gets the screens ------------------------------------------------------------------
@pytest.mark.parametrize("who", ["edd", "vi"])
def test_only_an_admin_is_given_the_screens(server: str, world: World, who: str) -> None:
    tab = admin_on(server, world, "members", who)
    try:
        tab.wait_for(lambda m: m["type"] == "init", 5)
        text = json.dumps(tab.messages)
        for screen in ("Members", "Tokens", "Audit", "Usage", "Invitation link", "New token"):
            assert screen not in text, screen
        assert world.people["ada"] not in text
        assert "ada@example.com" not in text
    finally:
        tab.close()


def test_an_admin_sees_the_links_and_the_people_of_their_workspace_only(
    server: str, world: World
) -> None:
    tab = admin_on(server, world, "members")
    try:
        until(lambda: "edd@example.com" in json.dumps(tab.messages), "members listed")
        for link in ("Members", "Tokens", "Audit", "Usage"):
            tab.find("link", label=link)
        assert "vi@example.com" in json.dumps(tab.messages)
    finally:
        tab.close()


# --- members -------------------------------------------------------------------------------
def test_an_admin_changes_a_role_and_the_service_agrees(server: str, world: World) -> None:
    tab = admin_on(server, world, "members")
    try:
        until(lambda: "vi@example.com" in json.dumps(tab.messages), "members listed")
        act(tab, "Members", "a1", world.people["vi"])  # Make editor
        until(lambda: world.service.membership_of(world.people["vi"], "acme") == "editor", "role")
        assert any(e["action"] == "member.role" for e in world.acme.list_audit())
    finally:
        tab.close()


def test_the_last_admin_cannot_demote_themselves_and_is_told_why(server: str, world: World) -> None:
    tab = admin_on(server, world, "members")
    try:
        until(lambda: "ada@example.com" in json.dumps(tab.messages), "members listed")
        mark = tab.mark()
        act(tab, "Members", "a0", world.people["ada"])  # Make viewer
        until(lambda: error_shown(tab, mark, "last_admin"), "the refusal")
        assert world.service.membership_of(world.people["ada"], "acme") == "admin"
    finally:
        tab.close()


def test_removing_a_member_takes_them_out_and_ends_their_sign_in_here(
    server: str, world: World
) -> None:
    tab = admin_on(server, world, "members")
    try:
        until(lambda: "edd@example.com" in json.dumps(tab.messages), "members listed")
        act(tab, "Members", "a3", world.people["edd"])  # Remove
        until(lambda: world.service.membership_of(world.people["edd"], "acme") is None, "removed")
        assert world.people["edd"] not in {m["person_id"] for m in world.acme.list_members()}
        session = world.service.authenticate_session(world.sessions["edd"][COOKIE])
        assert session is not None  # still signed in as a person...
        assert world.service.principal_for_session(session) is None  # ...but in no workspace
    finally:
        tab.close()


def test_an_invitation_link_is_shown_once_to_this_tab_and_works_once(
    server: str, world: World
) -> None:
    tab = admin_on(server, world, "members")
    try:
        until(lambda: "ada@example.com" in json.dumps(tab.messages), "members listed")
        type_into(tab, "textinput", "Email", "New.Person@Example.com")
        type_into(tab, "select", "Role", "editor")
        mark = tab.mark()
        tab.send(tab.find("button", label="Send invitation"), "click")
        link = until(lambda: revealed(tab, mark, "Invitation link"), "the link")
        assert (
            link.startswith("http://127.0.0.1:8080/auth/invite/smla_inv_")
            or "/auth/invite/smla_inv_" in link
        )
        token = link.rsplit("/", 1)[1]
        # the secret is in no snapshot and in nothing a second tab is sent
        other = admin_on(server, world, "members")
        try:
            until(lambda: "ada@example.com" in json.dumps(other.messages), "second tab loaded")
            assert token not in json.dumps(other.messages) and token not in json.dumps(other.init)
        finally:
            other.close()
        pending = [
            i for i in world.acme.list_invitations() if i["email"] == "new.person@example.com"
        ]
        assert pending and pending[0]["role"] == "editor" and pending[0]["status"] == "open"
        newcomer = world.service.sign_in_identity(
            Identity("https://idp.test", "np", "new.person@example.com", True, "New")
        )
        world.service.accept_invitation(token, newcomer["id"])
        assert world.service.membership_of(newcomer["id"], "acme") == "editor"
        with pytest.raises(SemulaError):
            world.service.accept_invitation(token, newcomer["id"])  # single use
    finally:
        tab.close()


def test_a_bad_invitation_is_refused_with_its_code_and_nothing_is_created(
    server: str, world: World
) -> None:
    tab = admin_on(server, world, "members")
    try:
        until(lambda: "ada@example.com" in json.dumps(tab.messages), "members listed")
        before = world.acme.list_invitations()
        type_into(tab, "textinput", "Email", "not-an-address")
        mark = tab.mark()
        tab.send(tab.find("button", label="Send invitation"), "click")
        until(lambda: error_shown(tab, mark, "invalid_email"), "the refusal")
        assert world.acme.list_invitations() == before
    finally:
        tab.close()


def test_an_open_invitation_can_be_revoked(server: str, world: World) -> None:
    made = world.acme.create_invitation("later@example.com", "viewer")
    tab = admin_on(server, world, "members")
    try:
        until(lambda: "later@example.com" in json.dumps(tab.messages), "invitation listed")
        act(tab, "Open invitations", "a0", made["id"])
        until(lambda: world.acme.list_invitations()[0]["status"] == "revoked", "revoked")
    finally:
        tab.close()


# --- tokens --------------------------------------------------------------------------------
def test_creating_a_token_shows_its_secret_once_and_it_works_with_its_scope_and_project(
    server: str, world: World
) -> None:
    tab = admin_on(server, world, "tokens")
    try:
        until(lambda: tab.find("table", label="Tokens"), "page built")
        type_into(tab, "textinput", "Name", "ci runner")
        type_into(tab, "select", "Scope", "replay")
        type_into(tab, "select", "Project", "shop")
        mark = tab.mark()
        tab.send(tab.find("button", label="Create token"), "click")
        secret = until(lambda: revealed(tab, mark, "New token"), "the secret")
        assert secret.startswith("smla_live_")
        made = [t for t in world.acme.list_tokens() if t["name"] == "ci runner"]
        assert len(made) == 1 and made[0]["scope"] == "replay" and made[0]["project"] == "shop"
        who = verify(world, secret)
        assert who is not None and who.workspace_id == "acme" and who.scope == "replay"
        assert secret not in json.dumps(tab.init)
        carriers = [m for m in tab.messages if secret in json.dumps(m)]
        assert len(carriers) == 1  # only the patch to the SecretReveal ever held it
    finally:
        tab.close()


def test_a_token_needs_a_name_and_the_refusal_is_shown(server: str, world: World) -> None:
    tab = admin_on(server, world, "tokens")
    try:
        until(lambda: tab.find("table", label="Tokens"), "page built")
        mark = tab.mark()
        tab.send(tab.find("button", label="Create token"), "click")
        until(lambda: error_shown(tab, mark, "invalid_name"), "the refusal")
        assert world.acme.list_tokens() == []
    finally:
        tab.close()


def test_rotating_a_token_makes_a_new_one_and_lets_the_old_one_run_out(
    server: str, world: World
) -> None:
    old = world.acme.create_token("deploy", "record")
    tab = admin_on(server, world, "tokens")
    try:
        until(lambda: "deploy" in json.dumps(tab.messages), "token listed")
        mark = tab.mark()
        act(tab, "Tokens", "a0", old["id"])  # Rotate
        secret = until(lambda: revealed(tab, mark, "New token"), "the new secret")
        assert secret != old["token"] and verify(world, secret) is not None
        assert verify(world, old["token"]) is not None  # grace period
        after = {t["id"]: t for t in world.acme.list_tokens()}
        assert len(after) == 2 and after[old["id"]]["expires_at"] is not None
    finally:
        tab.close()


def test_revoking_a_token_stops_it_at_once(server: str, world: World) -> None:
    made = world.acme.create_token("deploy", "admin")
    assert verify(world, made["token"]) is not None
    tab = admin_on(server, world, "tokens")
    try:
        until(lambda: "deploy" in json.dumps(tab.messages), "token listed")
        act(tab, "Tokens", "a1", made["id"])  # Revoke
        until(lambda: world.acme.list_tokens()[0]["revoked_at"], "revoked")
        assert verify(world, made["token"]) is None
    finally:
        tab.close()


def test_a_tab_cannot_touch_another_workspaces_token(server: str, world: World) -> None:
    from semula.scope import Scope

    theirs = world.service.for_scope(Scope("rival")).create_token("theirs", "admin")
    tab = admin_on(server, world, "tokens")
    try:
        until(lambda: tab.find("table", label="Tokens"), "page built")
        mark = tab.mark()
        act(tab, "Tokens", "a1", theirs["id"])  # Revoke somebody else's
        time.sleep(0.8)
        assert verify(world, theirs["token"]) is not None
        assert "theirs" not in json.dumps(tab.messages) and "theirs" not in tab.since(mark)
    finally:
        tab.close()


# --- audit ---------------------------------------------------------------------------------
def test_the_audit_log_pages_back_and_filters_by_action(server: str, world: World) -> None:
    for _ in range(55):  # 110 role changes, so there are three pages
        world.acme.set_member_role(world.people["vi"], "editor")
        world.acme.set_member_role(world.people["vi"], "viewer")
    total = len(world.acme.list_audit(500))
    assert total > 110
    tab = admin_on(server, world, "audit")
    try:
        until(lambda: "member.role" in json.dumps(tab.messages), "the log")
        first = until(lambda: _table_ids(tab), "first page")
        assert len(first) == 50 and [int(i) for i in first] == sorted(map(int, first), reverse=True)
        tab.send(tab.find("button", label="Older"), "click")
        second = until(lambda: _table_ids(tab, not_in=first), "second page")
        assert len(second) == 50 and max(map(int, second)) < min(
            map(int, first)
        )  # no row twice, none skipped
        assert set(first) | set(second) == {str(e["id"]) for e in world.acme.list_audit(100)}
        tab.send(tab.find("button", label="Newer"), "click")
        assert until(lambda: _table_ids(tab, not_in=second), "back") == first
        type_into(tab, "select", "Action", "invitation.accept")
        only = until(lambda: _table_ids(tab, not_in=first), "filtered")
        assert len(only) == 3 and len(only) == len(
            world.acme.list_audit(500, action="invitation.accept")
        )
    finally:
        tab.close()


def _table_ids(tab: Tab, not_in: list[str] | None = None) -> list[str]:
    """The ids on the audit table now, from the last ``set`` patch aimed at it."""
    table = tab.find("table", label="Audit log")
    rows: list[str] = []
    for m in tab.messages:
        if m["type"] != "patch":
            continue
        for c in m["changes"]:
            if c.get("target") == table["id"] and isinstance(c.get("props", {}).get("rows"), list):
                rows = [str(r["id"]) for r in c["props"]["rows"]]
    return rows if rows and rows != (not_in or []) else []


# --- usage and deletion --------------------------------------------------------------------
def test_usage_shows_what_the_workspace_holds(server: str, world: World) -> None:
    tab = admin_on(server, world, "usage")
    try:
        until(lambda: "Stored text" in json.dumps(tab.messages), "usage lines")
        text = json.dumps(tab.messages)
        for label in ("Projects", "Entries", "Parked calls", "Active tokens"):
            assert label in text
    finally:
        tab.close()


def test_deleting_the_workspace_needs_its_id_typed_back(server: str, world: World) -> None:
    token = world.acme.create_token("deploy", "admin")
    tab = admin_on(server, world, "usage")
    try:
        until(lambda: tab.find("button", label="Delete workspace"), "page built")
        type_into(tab, "textinput", "Workspace id", "acm")
        mark = tab.mark()
        tab.send(tab.find("button", label="Delete workspace"), "click")
        until(lambda: error_shown(tab, mark, "confirmation_mismatch"), "the refusal")
        assert world.acme.workspace_status()["deleted_at"] is None
        assert verify(world, token["token"]) is not None

        type_into(tab, "textinput", "Workspace id", "acme")
        tab.send(tab.find("button", label="Delete workspace"), "click")
        until(lambda: world.acme.workspace_status()["deleted_at"], "deleted")
        assert verify(world, token["token"]) is None  # its tokens stopped
        assert any(e["action"] == "workspace.delete" for e in world.acme.list_audit())
    finally:
        tab.close()
