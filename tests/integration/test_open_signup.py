"""Open signup (SEM-123): with ``SEMULA_SIGNUP=open`` any signed-in person (whose email the provider
verified) makes a workspace of their own, within a per-address and a per-day limit. The default
stays invite-code only, and the limits are kept in the database so a restart does not reset them."""

import threading
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from semula import schema
from semula.app import create_app
from semula.config import Settings
from semula.db import Database
from semula.errors import SemulaError
from semula.oidc import Identity, Provider
from semula.scope import Principal, Scope
from semula.service import Service
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration

SECRET = "a-secret-of-at-least-sixteen-bytes"  # noqa: S105
PROVIDER = Provider("google", "oidc", "client", "secret", issuer="http://127.0.0.1:1")


class World:
    def __init__(self, tmp_path: Path, **settings: object) -> None:
        self.tmp = tmp_path
        self.url = fresh_database_url(tmp_path)
        self.settings = Settings(
            data_dir=tmp_path,
            hosted=True,
            secret=SECRET,
            auth_providers={"google": PROVIDER},
            database_url=self.url,
            **settings,  # type: ignore[arg-type]
        )
        self.service = Service(Database(self.url), self.settings)

    def person(self, name: str) -> str:
        who = self.service.sign_in_identity(
            Identity("https://idp.test", name, f"{name}@x.test", True, name.title())
        )
        return str(who["id"])

    def refused(self, person: str, address: str = "203.0.113.7") -> SemulaError:
        with pytest.raises(SemulaError) as caught:
            self.service.signup_open(person, address)
        return caught.value

    def backdate(self) -> None:
        """Make every open signup so far a long time ago."""
        self.service.db.for_workspace(None).execute(
            sa.update(schema.signup_open).values(created_at="2000-01-01T00:00:00Z")
        )

    def site(self) -> httpx.AsyncClient:
        app = create_app(self.settings)
        app.state.service = self.service
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 5000)),
            base_url="http://127.0.0.1:8080",
        )

    def signed_in(self, name: str) -> tuple[str, dict[str, str], str]:
        person = self.person(name)
        cookie, session = self.service.create_session(person)
        return person, {"semula_session": cookie}, session["csrf"]


# --- the service ---------------------------------------------------------------------------


def test_the_default_is_invite_codes_only_and_open_signup_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path)
    assert world.settings.signup == "code"
    ada = world.person("ada")
    err = world.refused(ada)
    assert (err.status, err.code) == (403, "signup_closed")
    assert world.service.workspaces_of(ada) == []


def test_an_open_server_gives_a_signed_in_person_a_workspace_of_their_own(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open")
    ada = world.person("ada")
    got = world.service.signup_open(ada, "203.0.113.7")
    assert world.service.membership_of(ada, got["workspace_id"]) == "admin"
    assert got["workspace"] == "Ada's workspace"
    mine = world.service.for_scope(
        Scope(got["workspace_id"], Principal("person", ada, got["workspace_id"], role="admin"))
    )
    assert [p["id"] for p in mine.list_projects()] == ["default"]
    assert {"workspace.create", "signup.open"} <= {a["action"] for a in mine.list_audit()}


def test_a_person_signs_up_once(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open")
    ada = world.person("ada")
    world.service.signup_open(ada, "203.0.113.7")
    err = world.refused(ada)
    assert (err.status, err.code) == (409, "already_signed_up")
    assert len(world.service.workspaces_of(ada)) == 1


def test_only_the_server_itself_signs_people_up(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open")
    ada = world.person("ada")
    world.service.create_workspace("acme")
    acting = Principal("person", ada, "acme", role="admin")
    scoped = world.service.for_scope(Scope("acme", acting))
    with pytest.raises(SemulaError) as caught:
        scoped.signup_open(ada, "203.0.113.7")
    assert caught.value.status == 403
    assert world.service.workspaces_of(ada) == []


def test_two_tabs_signing_up_at_once_make_one_workspace(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open")
    ada = world.person("ada")
    outcomes: list[str] = []

    def go() -> None:
        try:
            world.service.signup_open(ada, "203.0.113.7")
            outcomes.append("made")
        except SemulaError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["already_signed_up", "made"]
    assert len(world.service.workspaces_of(ada)) == 1


def test_an_unverified_email_never_gets_as_far_as_signing_up(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open")
    with pytest.raises(SemulaError) as caught:
        world.service.sign_in_identity(Identity("https://idp.test", "x", "x@x.test", False, "X"))
    assert caught.value.code == "email_not_verified"


def test_one_address_can_only_make_so_many_workspaces_a_day(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open", signup_per_address=2, signup_per_day=None)
    for name in ("ada", "bob"):
        world.service.signup_open(world.person(name), "203.0.113.7")
    cat = world.person("cat")
    err = world.refused(cat)
    assert (err.status, err.code) == (429, "signup_limit")
    assert world.service.workspaces_of(cat) == []  # nothing was made, and cat can try elsewhere
    world.service.signup_open(cat, "198.51.100.9")
    # a day later the address may make more
    world.backdate()
    dan = world.person("dan")
    world.service.signup_open(dan, "203.0.113.7")


def test_an_ipv6_network_counts_as_one_address(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open", signup_per_address=1, signup_per_day=None)
    world.service.signup_open(world.person("ada"), "2001:db8:1:2::1")
    assert world.refused(world.person("bob"), "2001:db8:1:2:ffff::9").code == "signup_limit"
    world.service.signup_open(world.person("cat"), "2001:db8:1:3::1")  # another /64


def test_the_whole_server_makes_only_so_many_workspaces_a_day(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open", signup_per_address=None, signup_per_day=2)
    world.service.signup_open(world.person("ada"), "203.0.113.1")
    world.service.signup_open(world.person("bob"), "203.0.113.2")
    cat = world.person("cat")
    err = world.refused(cat, "203.0.113.3")
    assert (err.status, err.code) == (429, "signup_limit")
    world.backdate()
    world.service.signup_open(cat, "203.0.113.3")


def test_none_removes_the_limits(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open", signup_per_address=None, signup_per_day=None)
    for i in range(6):
        world.service.signup_open(world.person(f"p{i}"), "203.0.113.7")


def test_a_person_with_a_suspended_workspace_cannot_start_over(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open")
    ada = world.person("ada")
    first = world.service.signup_open(ada, "203.0.113.7")
    bob = world.person("bob")
    world.service.create_workspace("elsewhere")
    elsewhere = world.service.for_scope(Scope("elsewhere"))
    world.service.accept_invitation(
        elsewhere.create_invitation("bob@x.test", "admin")["token"], bob
    )
    elsewhere.suspend_workspace("spam")
    err = world.refused(bob)
    assert err.code == "workspace_suspended" and err.status == 403
    assert first["workspace_id"] and world.service.workspaces_of(bob) == []


# --- the page ------------------------------------------------------------------------------


async def test_the_page_offers_a_workspace_without_a_code(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open")
    person, cookies, csrf = world.signed_in("ada")
    async with world.site() as c:
        c.cookies.update(cookies)
        page = await c.get("/auth/signup")
        assert page.status_code == 200 and "required" not in page.text.split("name=code")[1][:80]
        done = await c.post("/auth/signup", data={"code": "", "csrf": csrf})
        assert done.status_code == 303 and done.headers["location"] == "/ui/"
    assert len(world.service.workspaces_of(person)) == 1


async def test_in_code_mode_the_page_still_needs_a_code(tmp_path: Path) -> None:
    world = World(tmp_path)
    person, cookies, csrf = world.signed_in("ada")
    async with world.site() as c:
        c.cookies.update(cookies)
        page = await c.get("/auth/signup")
        assert "required" in page.text.split("name=code")[1][:80]
        refused = await c.post("/auth/signup", data={"code": "", "csrf": csrf})
        assert refused.status_code == 404
    assert world.service.workspaces_of(person) == []


async def test_an_invite_code_still_works_when_signup_is_open(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open")
    person, cookies, csrf = world.signed_in("ada")
    code = world.service.create_signup_code()["code"]
    async with world.site() as c:
        c.cookies.update(cookies)
        done = await c.post("/auth/signup", data={"code": code, "csrf": csrf})
        assert done.status_code == 303
    assert len(world.service.workspaces_of(person)) == 1


async def test_a_limit_is_shown_as_a_page_not_an_error(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open", signup_per_address=1, signup_per_day=None)
    _, cookies, csrf = world.signed_in("ada")
    _, cookies2, csrf2 = world.signed_in("bob")
    async with world.site() as c:
        c.cookies.update(cookies)
        assert (await c.post("/auth/signup", data={"code": "", "csrf": csrf})).status_code == 303
    async with world.site() as c:
        c.cookies.update(cookies2)
        blocked = await c.post("/auth/signup", data={"code": "", "csrf": csrf2})
        assert blocked.status_code == 429 and "text/html" in blocked.headers["content-type"]
        assert "tomorrow" in blocked.text.lower()


async def test_open_signup_still_needs_the_session_csrf_token(tmp_path: Path) -> None:
    world = World(tmp_path, signup="open")
    person, cookies, _ = world.signed_in("ada")
    async with world.site() as c:
        c.cookies.update(cookies)
        assert (await c.post("/auth/signup", data={"code": "", "csrf": "x"})).status_code == 403
    assert world.service.workspaces_of(person) == []


# --- the setting ---------------------------------------------------------------------------


def test_the_setting_flips_with_no_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEMULA_SIGNUP", raising=False)
    assert Settings.from_env().signup == "code"
    monkeypatch.setenv("SEMULA_SIGNUP", "open")
    assert Settings.from_env().signup == "open"
    monkeypatch.setenv("SEMULA_SIGNUP", "anyone")
    with pytest.raises(ValueError, match="SEMULA_SIGNUP"):
        Settings.from_env()


def test_the_creation_limits_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.from_env().signup_per_address == 3
    assert Settings.from_env().signup_per_day == 100
    monkeypatch.setenv("SEMULA_SIGNUP_PER_ADDRESS", "5")
    monkeypatch.setenv("SEMULA_SIGNUP_PER_DAY", "none")
    got = Settings.from_env()
    assert got.signup_per_address == 5 and got.signup_per_day is None
