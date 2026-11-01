"""Invite-code signup: a signed-in person with no workspace redeems a code the operator made and
gets one of their own (SEM-117). Codes are single purpose, hashed, audited and never shown again."""

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from semula import schema
from semula.app import create_app
from semula.cli import main
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
SAME = "That code is not valid any more."


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.url = fresh_database_url(tmp_path)
        self.service = Service(Database(self.url), Settings(data_dir=tmp_path, hosted=True))

    def person(self, name: str) -> str:
        who = self.service.sign_in_identity(
            Identity("https://idp.test", name, f"{name}@x.test", True, name.title())
        )
        return str(who["id"])

    def refused(self, secret: str, person: str) -> SemulaError:
        with pytest.raises(SemulaError) as caught:
            self.service.redeem_signup_code(secret, person)
        return caught.value


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_the_operator_makes_a_code_that_is_shown_once_and_stored_hashed(world: World) -> None:
    made = world.service.create_signup_code(uses=3, days=7, note="beta list")
    assert made["code"].startswith("smla_code_") and made["uses"] == 3
    listed = world.service.list_signup_codes()
    assert [c["id"] for c in listed] == [made["id"]]
    assert "code" not in listed[0] and listed[0]["note"] == "beta list" and listed[0]["used"] == 0
    stored = world.service.db.for_workspace(None).rows(sa.select(schema.signup_code))
    assert made["code"] not in json.dumps([dict(r) for r in stored], default=str)


def test_redeeming_gives_the_person_a_workspace_of_their_own_as_its_admin(world: World) -> None:
    ada = world.person("ada")
    code = world.service.create_signup_code()["code"]
    got = world.service.redeem_signup_code(code, ada)
    assert world.service.membership_of(ada, got["workspace_id"]) == "admin"
    assert [w["workspace_id"] for w in world.service.workspaces_of(ada)] == [got["workspace_id"]]
    mine = world.service.for_scope(
        Scope(got["workspace_id"], Principal("person", ada, got["workspace_id"], role="admin"))
    )
    assert [p["id"] for p in mine.list_projects()] == ["default"]
    actions = {a["action"] for a in mine.list_audit()}
    assert {"workspace.create", "signup.redeem"} <= actions


def test_each_person_gets_a_workspace_of_their_own(world: World) -> None:
    code = world.service.create_signup_code(uses=2)["code"]
    one = world.service.redeem_signup_code(code, world.person("ada"))
    two = world.service.redeem_signup_code(code, world.person("bob"))
    assert one["workspace_id"] != two["workspace_id"]


@pytest.mark.parametrize("why", ["unknown", "expired", "revoked", "used"])
def test_a_code_that_does_not_work_says_the_same_thing_whatever_the_reason(
    world: World, monkeypatch: pytest.MonkeyPatch, why: str
) -> None:
    ada, bob = world.person("ada"), world.person("bob")
    made = world.service.create_signup_code(uses=1, days=1)
    secret = made["code"]
    if why == "unknown":
        secret = "smla_code_" + "x" * 32
    elif why == "expired":
        later = datetime.now(UTC) + timedelta(days=2)
        monkeypatch.setattr(world.service, "_now", lambda: later)
    elif why == "revoked":
        world.service.revoke_signup_code(made["id"])
    else:
        world.service.redeem_signup_code(secret, bob)
    err = world.refused(secret, ada)
    assert (err.status, err.code, err.message) == (404, "signup_code_invalid", SAME)
    assert world.service.workspaces_of(ada) == []


def test_a_person_cannot_redeem_a_second_time(world: World) -> None:
    ada = world.person("ada")
    world.service.redeem_signup_code(world.service.create_signup_code()["code"], ada)
    err = world.refused(world.service.create_signup_code()["code"], ada)
    assert (err.status, err.code) == (409, "already_signed_up")
    assert len(world.service.workspaces_of(ada)) == 1


def test_a_single_use_code_goes_to_one_of_two_people_racing_for_it(world: World) -> None:
    code = world.service.create_signup_code(uses=1)["code"]
    people = [world.person(f"p{i}") for i in range(6)]
    results: list[str] = []
    gate = threading.Barrier(len(people))

    def go(person: str) -> None:
        gate.wait()
        try:
            world.service.redeem_signup_code(code, person)
            results.append("won")
        except SemulaError:
            results.append("lost")

    threads = [threading.Thread(target=go, args=(p,)) for p in people]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("won") == 1
    assert (
        len(world.service.db.for_workspace(None).rows(sa.select(schema.workspace))) == 2
    )  # +default


def test_only_the_operator_makes_codes(world: World) -> None:
    ada = world.person("ada")
    world.service.create_workspace("acme")
    as_admin = world.service.for_scope(
        Scope("acme", Principal("person", ada, "acme", role="admin"))
    )
    for call in (
        lambda: as_admin.create_signup_code(),
        lambda: as_admin.list_signup_codes(),
        lambda: as_admin.revoke_signup_code("x"),
    ):
        with pytest.raises(SemulaError) as caught:
            call()
        assert caught.value.status == 403


def test_codes_have_sane_bounds(world: World) -> None:
    for kwargs in ({"uses": 0}, {"uses": 10_001}, {"days": 0}, {"days": 366}, {"note": "x" * 201}):
        with pytest.raises(SemulaError) as caught:
            world.service.create_signup_code(**kwargs)
        assert caught.value.status == 422


def test_an_unknown_signup_mode_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMULA_SIGNUP", "anyone")
    with pytest.raises(ValueError, match="SEMULA_SIGNUP"):
        Settings.from_env()
    monkeypatch.setenv("SEMULA_SIGNUP", "code")
    assert Settings.from_env().signup == "code"


# --- the page ------------------------------------------------------------------------------


def site(world: World) -> httpx.AsyncClient:
    app = create_app(
        Settings(
            data_dir=world.tmp,
            hosted=True,
            secret=SECRET,
            auth_providers={"google": PROVIDER},
            database_url=world.url,
        )
    )
    app.state.service = world.service
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
    )


def signed_in(world: World, name: str = "ada") -> tuple[str, dict[str, str], str]:
    person = world.person(name)
    cookie, session = world.service.create_session(person)
    return person, {"semula_session": cookie}, session["csrf"]


async def test_the_page_asks_a_signed_out_person_to_sign_in_first(world: World) -> None:
    async with site(world) as c:
        resp = await c.get("/auth/signup")
        assert resp.status_code == 302 and resp.headers["location"].startswith("/auth/login?")


async def test_a_signed_in_person_redeems_a_code_and_lands_in_their_workspace(
    world: World,
) -> None:
    person, cookies, csrf = signed_in(world)
    code = world.service.create_signup_code()["code"]
    async with site(world) as c:
        c.cookies.update(cookies)
        page = await c.get("/auth/signup")
        assert page.status_code == 200 and csrf in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        done = await c.post("/auth/signup", data={"code": code, "csrf": csrf})
        assert done.status_code == 303 and done.headers["location"] == "/ui/"
    workspaces = world.service.workspaces_of(person)
    assert len(workspaces) == 1 and workspaces[0]["role"] == "admin"


async def test_the_page_needs_the_session_csrf_token_and_its_own_origin(world: World) -> None:
    _, cookies, csrf = signed_in(world)
    code = world.service.create_signup_code()["code"]
    async with site(world) as c:
        c.cookies.update(cookies)
        wrong = await c.post("/auth/signup", data={"code": code, "csrf": "nope"})
        assert wrong.status_code == 403
        other = await c.post(
            "/auth/signup",
            data={"code": code, "csrf": csrf},
            headers={"Origin": "http://evil.test"},
        )
        assert other.status_code == 403
    assert world.service.workspaces_of(signed_in(world, "bob")[0]) == []  # nothing was made


async def test_a_bad_code_gets_the_same_page_whatever_is_wrong_with_it(world: World) -> None:
    _, cookies, csrf = signed_in(world)
    async with site(world) as c:
        c.cookies.update(cookies)
        bad = await c.post("/auth/signup", data={"code": "smla_code_nope", "csrf": csrf})
        assert bad.status_code == 404 and SAME in bad.text
        blank = await c.post("/auth/signup", data={"code": "", "csrf": csrf})
        assert blank.status_code == 404 and SAME in blank.text


# --- the operator's command -----------------------------------------------------------------


def test_the_operator_makes_lists_and_revokes_codes_from_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEMULA_DATABASE_URL", fresh_database_url(tmp_path))
    monkeypatch.setenv("SEMULA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEMULA_PUBLIC_URL", "https://semula.example.com")
    assert main(["--json", "admin", "signup-code-create", "--uses", "5", "--note", "beta"]) == 0
    made = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (
        made["code"].startswith("smla_code_")
        and made["page"] == "https://semula.example.com/auth/signup"
    )
    assert main(["--json", "admin", "signup-code-list"]) == 0
    listed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert listed[0]["id"] == made["id"] and "code" not in listed[0]
    assert main(["admin", "signup-code-revoke", made["id"]]) == 0
    assert main(["--json", "admin", "signup-code-list"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])[0]["revoked"] is True
    assert main(["admin", "signup-code-revoke", "nope"]) == 4
