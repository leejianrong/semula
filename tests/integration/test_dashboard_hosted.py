"""The dashboard behind sign-in: who sees what, and what a role may do (SEM-91)."""

import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from semula import canonical
from semula.config import Settings
from semula.db import Database
from semula.oidc import Identity, Provider
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url
from tests.support.server import running_server
from tests.support.ui import Tab

pytestmark = pytest.mark.integration

PROVIDER = Provider("google", "oidc", "client", "secret", issuer="https://idp.test")
SECRET = "a-secret-of-at-least-sixteen-bytes"  # noqa: S105
COOKIE = "semula_session"  # http: no __Host- prefix


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.db_url = fresh_database_url(tmp_path)
        self.service = Service(Database(self.db_url), Settings(data_dir=tmp_path, hosted=True))
        self.service.create_workspace("acme")
        self.service.create_workspace("rival")
        self.acme = self.service.for_scope(Scope("acme"))
        self.people: dict[str, str] = {}
        self.sessions: dict[str, dict[str, str]] = {}
        for name, role in (("ada", "admin"), ("edd", "editor"), ("vi", "viewer")):
            who = self.service.sign_in_identity(
                Identity("https://idp.test", name, f"{name}@example.com", True, name.title())
            )
            self.people[name] = who["id"]
            invite = self.acme.create_invitation(f"{name}@example.com", role)
            self.service.accept_invitation(invite["token"], who["id"])
            secret, _ = self.service.create_session(who["id"])
            self.sessions[name] = {COOKIE: secret}
        self.acme.create_project("shop")
        self.pending = self.acme.open_pending(
            canonical.from_openai({"messages": [{"role": "user", "content": "held for a person"}]}),
            "shop",
        )
        self.entry, _ = self.acme.upsert_entry(
            "shop",
            {"messages": [{"role": "user", "content": "a recorded prompt"}]},
            {"content": "x"},
        )
        rival = self.service.for_scope(Scope("rival"))
        rival.create_project("shop")
        rival.open_pending(
            canonical.from_openai({"messages": [{"role": "user", "content": "rival secret"}]}),
            "shop",
        )


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


def open_as(server: str, world: World, who: str, path: str = "/projects/shop/pending") -> Tab:
    return Tab(server, path, cookies=world.sessions[who])


def saw(tab: Tab, text: str) -> bool:
    return text in str(list(tab.nodes())) or tab.saw(text)


def test_a_page_load_without_a_sign_in_goes_to_the_sign_in_page(server: str) -> None:
    resp = httpx.get(server + "/ui/projects/shop/pending", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/auth/login?next=%2Fui%2Fprojects%2Fshop%2Fpending"
    root = httpx.get(server + "/ui/", follow_redirects=False)
    assert root.status_code == 302 and root.headers["location"].startswith("/auth/login")


def test_the_apps_own_calls_are_refused_without_a_sign_in(server: str) -> None:
    assert httpx.post(server + "/ui/api/session", json={}).status_code == 401
    event = {"sid": "x", "component": "n1", "event": "click", "payload": {}}
    assert httpx.post(server + "/ui/api/event", json=event).status_code == 401


def test_a_forged_or_borrowed_session_id_is_refused(server: str, world: World) -> None:
    mine = httpx.post(
        server + "/ui/api/session",
        json={},
        cookies=world.sessions["ada"],
        headers={"Origin": server},
    ).json()["token"]
    theirs = world.sessions["vi"]
    event = {"sid": mine, "component": "n1", "event": "click", "payload": {}}
    borrowed = httpx.post(
        server + "/ui/api/event", json=event, cookies=theirs, headers={"Origin": server}
    )
    assert borrowed.status_code == 403  # a token minted for Ada is not Vi's
    other_site = httpx.post(
        server + "/ui/api/event", json=event, cookies=world.sessions["ada"],
        headers={"Origin": "https://evil.example"},
    )  # fmt: skip
    assert other_site.status_code in (400, 403)


def test_a_signed_in_person_sees_their_own_workspace_and_none_other(
    server: str, world: World
) -> None:
    tab = open_as(server, world, "ada")
    try:
        tab.wait_for(lambda m: "held for a person" in str(m), 5)
        assert saw(tab, "ada@example.com") or saw(tab, "Ada")
        assert "rival secret" not in str(tab.messages) and not saw(tab, "rival secret")
    finally:
        tab.close()


def test_a_viewer_can_read_but_the_edit_controls_are_not_even_sent(
    server: str, world: World
) -> None:
    tab = open_as(server, world, "vi")
    try:
        tab.wait_for(lambda m: "held for a person" in str(m), 5)
        for label in ("Send answer", "New entry", "Approve", "Reject"):
            with pytest.raises(LookupError):
                tab.find("button", label=label)
        for label in ("Record",):
            with pytest.raises(LookupError):
                tab.find("switch", label=label)
        assert "Playground" not in str(list(tab.nodes()))
        assert "Sign out" in str(list(tab.nodes()))
    finally:
        tab.close()


def test_an_editor_gets_the_editing_controls_and_not_the_admin_screens(
    server: str, world: World
) -> None:
    tab = open_as(server, world, "edd")
    try:
        tab.wait_for(lambda m: "held for a person" in str(m), 5)
        tab.find("button", label="Send answer")
        tab.find("switch", label="Record")
        assert "Playground" in str(list(tab.nodes()))
        for admin_screen in ("Members", "Tokens", "Audit"):
            assert admin_screen not in str(list(tab.nodes())), admin_screen
    finally:
        tab.close()


def test_an_editor_answers_a_parked_call_as_a_person(server: str, world: World) -> None:
    tab = open_as(server, world, "edd")
    try:
        tab.wait_for(lambda m: "held for a person" in str(m), 5)
        tab.send(tab.find("table", label="Parked calls"), "select", {"key": world.pending})
        time.sleep(0.5)
        tab.send(
            tab.find("codeeditor", label="Answer"), "input", {"value": "answered in the browser"}
        )
        tab.send(tab.find("button", label="Send answer"), "click")
        for _ in range(50):
            if world.acme.get_pending(world.pending)["status"] == "answered":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the call was not answered")
        recorded = [
            e for e in world.acme.list_entries("shop")["entries"] if "held" in e["last_message"]
        ]
        assert recorded
        history = world.acme.entry_history(recorded[0]["id"])["revisions"][0]
        assert history["actor"] == "human"
    finally:
        tab.close()


def test_a_role_taken_away_while_the_dashboard_is_open_fails_closed_at_the_next_click(
    server: str, world: World
) -> None:
    tab = open_as(server, world, "edd")
    try:
        tab.wait_for(lambda m: "held for a person" in str(m), 5)
        world.acme.set_member_role(world.people["edd"], "viewer")  # an admin demotes them
        tab.send(tab.find("table", label="Parked calls"), "select", {"key": world.pending})
        time.sleep(0.5)
        tab.send(tab.find("codeeditor", label="Answer"), "input", {"value": "too late"})
        mark = tab.mark()
        tab.send(tab.find("button", label="Send answer"), "click")
        tab.wait_for(lambda m: "insufficient_role" in str(m), 5, mark)
        assert world.acme.get_pending(world.pending)["status"] == "pending"
    finally:
        tab.close()


def test_signing_out_ends_the_session_for_good(server: str, world: World) -> None:
    tab = open_as(server, world, "vi")
    try:
        tab.wait_for(lambda m: "held for a person" in str(m), 5)
        tab.send(tab.find("button", label="Sign out"), "click")
        time.sleep(0.8)
        assert world.service.authenticate_session(world.sessions["vi"][COOKIE]) is None
        again = httpx.get(
            server + "/ui/projects/shop/pending",
            cookies=world.sessions["vi"],
            follow_redirects=False,
        )
        assert again.status_code == 302 and again.headers["location"].startswith("/auth/login")
    finally:
        tab.close()


def test_a_person_with_no_workspace_sees_only_the_waiting_page(server: str, world: World) -> None:
    drifter = world.service.sign_in_identity(
        Identity("https://idp.test", "drift", "drift@example.com", True, "D")
    )
    secret, _ = world.service.create_session(drifter["id"])
    tab = Tab(server, "/projects/shop/pending", cookies={COOKIE: secret})
    try:
        tab.wait_for(lambda m: m["type"] == "init", 5)
        text = str(list(tab.nodes()))
        assert "No workspace yet" in text or tab.saw("No workspace yet")
        assert "/auth/signup" in text or tab.saw("/auth/signup")  # where an invite code goes
        assert "held for a person" not in str(tab.messages) and "Send answer" not in text
    finally:
        tab.close()


def test_the_shell_is_served_with_a_strict_content_security_policy(
    server: str, world: World
) -> None:
    page = httpx.get(server + "/ui/projects/shop/pending", cookies=world.sessions["ada"])
    assert page.status_code == 200
    csp = page.headers.get("content-security-policy", "")
    assert (
        "script-src" in csp
        and "nonce-" in csp
        and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    )
    assert (
        page.headers.get("x-frame-options") == "DENY"
        and page.headers.get("x-content-type-options") == "nosniff"
    )
