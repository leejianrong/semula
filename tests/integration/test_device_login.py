"""Sign the CLI in with a browser approval (SEM-118): the CLI asks for a code, a signed-in person
approves it in a browser, and the CLI is handed an API token once. Denied, expired and reused
codes get nothing; the token's secret is stored hashed and is never printed."""

import json
import stat
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
from tests.support.server import running_server

pytestmark = pytest.mark.integration

SECRET = "a-secret-of-at-least-sixteen-bytes"  # noqa: S105
PROVIDER = Provider("google", "oidc", "client", "secret", issuer="https://idp.test")


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.url = fresh_database_url(tmp_path)
        self.service = Service(Database(self.url), Settings(data_dir=tmp_path, hosted=True))
        self.service.create_workspace("acme")
        self.acme = self.service.for_scope(Scope("acme"))
        self.acme.create_project("app")

    def member(self, name: str, role: str = "admin", workspace: str = "acme") -> str:
        who = self.service.sign_in_identity(
            Identity("https://idp.test", name, f"{name}@x.test", True, name.title())
        )
        if workspace == "acme":
            self.service.accept_invitation(
                self.acme.create_invitation(f"{name}@x.test", role)["token"], who["id"]
            )
        return str(who["id"])

    def session(self, person: str) -> tuple[dict[str, str], str]:
        secret, made = self.service.create_session(person)
        return {"semula_session": secret}, made["csrf"]

    def approve(self, started: dict[str, str], person: str, yes: bool = True) -> dict[str, object]:
        return self.service.device_decide(started["user_code"], person, "acme", yes)

    def refused(self, call: object, *args: object) -> SemulaError:
        with pytest.raises(SemulaError) as caught:
            call(*args)  # type: ignore[operator]
        return caught.value

    def principal(self, secret: str) -> Principal | None:
        return self.service.authenticate_token(secret)

    def rows(self) -> list[dict[str, object]]:
        found = self.service.db.for_workspace(None).rows(sa.select(schema.device_login))
        return [dict(r) for r in found]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# --- the service ---------------------------------------------------------------------------


def test_the_cli_gets_a_short_code_for_the_person_and_a_secret_one_for_itself(
    world: World,
) -> None:
    started = world.service.device_start()
    assert len(started["user_code"]) == 9 and started["user_code"][4] == "-"
    assert started["device_code"] != started["user_code"] and started["expires_in"] > 0
    assert started["interval"] >= 1
    stored = json.dumps(world.rows(), default=str)
    assert started["device_code"] not in stored  # only its hash is kept


def test_an_approved_login_hands_the_cli_a_token_once(world: World) -> None:
    ada = world.member("ada")
    started = world.service.device_start(scope="record", project="app", name="semula CLI on box")
    assert world.service.device_poll(started["device_code"]) == {"status": "pending"}
    world.approve(started, ada)
    got = world.service.device_poll(started["device_code"])
    assert got["status"] == "approved"
    who = world.principal(got["token"])
    assert who and (who.workspace_id, who.scope, who.project) == ("acme", "record", "app")
    assert got["workspace_id"] == "acme" and got["project"] == "app" and got["expires_at"]
    # the secret was never stored, and only one poll gets it
    assert got["token"] not in json.dumps(world.rows(), default=str)
    assert world.refused(world.service.device_poll, started["device_code"]).status == 400
    tokens = [t for t in world.acme.list_tokens() if t["name"] == "semula CLI on box"]
    assert len(tokens) == 1
    made_by = world.service.db.for_workspace(None).one(
        sa.select(schema.api_token.c.created_by).where(schema.api_token.c.id == tokens[0]["id"])
    )
    assert made_by is not None and made_by["created_by"] == ada


def test_a_denied_login_gets_no_token(world: World) -> None:
    ada = world.member("ada")
    started = world.service.device_start()
    world.approve(started, ada, yes=False)
    assert world.service.device_poll(started["device_code"]) == {"status": "denied"}
    assert world.acme.list_tokens() == []
    # and the code cannot be approved afterwards
    assert world.refused(world.approve, started, ada).status == 404
    assert world.acme.list_tokens() == []


def test_an_expired_code_gets_nothing(world: World) -> None:
    ada = world.member("ada")
    started = world.service.device_start()
    world.service.db.for_workspace(None).execute(
        sa.update(schema.device_login).values(expires_at="2000-01-01T00:00:00Z")
    )
    assert world.refused(world.approve, started, ada).status == 404
    assert world.refused(world.service.device_poll, started["device_code"]).status == 400
    assert world.refused(world.service.device_preview, started["user_code"]).status == 404


def test_a_code_decides_once(world: World) -> None:
    ada, bob = world.member("ada"), world.member("bob")
    started = world.service.device_start()
    world.approve(started, ada)
    assert world.refused(world.approve, started, bob).status == 404
    assert world.service.device_poll(started["device_code"])["status"] == "approved"
    assert world.refused(world.approve, started, ada).status == 404


def test_unknown_codes_say_the_same_thing_as_expired_ones(world: World) -> None:
    ada = world.member("ada")
    a = world.refused(world.service.device_preview, "ZZZZ-ZZZZ")
    b = world.refused(world.service.device_decide, "ZZZZ-ZZZZ", ada, "acme", True)
    c = world.refused(world.service.device_poll, "nope")
    assert (a.status, a.code) == (b.status, b.code) == (404, "device_code_invalid")
    assert c.status == 400 and c.code == "device_code_invalid"


def test_the_token_never_has_more_than_the_person_may(world: World) -> None:
    viewer, editor, admin = (
        world.member(n, r) for n, r in (("v", "viewer"), ("e", "editor"), ("a", "admin"))
    )
    for person, wanted, got in (
        (viewer, "record", "replay"),
        (editor, "admin", "record"),
        (editor, "replay", "replay"),
        (admin, "admin", "admin"),
    ):
        started = world.service.device_start(scope=wanted)
        assert world.approve(started, person)["scope"] == got
        assert (
            world.principal(world.service.device_poll(started["device_code"])["token"]).scope == got
        )  # type: ignore[union-attr]


def test_someone_outside_the_workspace_cannot_approve_into_it(world: World) -> None:
    stranger = world.member("eve", workspace="elsewhere")
    started = world.service.device_start()
    assert world.refused(world.approve, started, stranger).status == 403
    assert world.service.device_poll(started["device_code"]) == {"status": "pending"}


def test_a_project_that_does_not_exist_cannot_be_approved(world: World) -> None:
    ada = world.member("ada")
    started = world.service.device_start(project="ghost")
    assert world.refused(world.approve, started, ada).status == 404
    assert world.service.device_poll(started["device_code"]) == {"status": "pending"}


def test_what_the_cli_asks_for_is_checked(world: World) -> None:
    assert world.refused(world.service.device_start, "root").status == 422
    assert world.refused(world.service.device_start, "record", "Not A Slug").status == 422
    assert world.refused(world.service.device_start, "record", None, "x" * 200).status == 422


def test_whoever_is_not_the_operator_has_no_business_with_a_pile_of_pending_codes(
    world: World,
) -> None:
    for _ in range(3):
        world.service.device_start()
    assert len(world.rows()) == 3
    old = "2000-01-01T00:00:00Z"
    world.service.db.for_workspace(None).execute(
        sa.update(schema.device_login).values(expires_at=old)
    )
    world.service.device_start()
    assert len(world.rows()) == 1  # the dead ones were swept away


def test_a_token_says_who_it_is_and_can_log_itself_out(world: World) -> None:
    ada = world.member("ada")
    started = world.service.device_start(scope="record", project="app")
    world.approve(started, ada)
    got = world.service.device_poll(started["device_code"])
    token = world.service.authenticate_token(got["token"])
    assert token is not None
    mine = world.service.for_scope(Scope("acme", token))
    me = mine.whoami()
    assert (me["workspace_id"], me["scope"], me["project"], me["id"]) == (
        "acme",
        "record",
        "app",
        token.id,
    )
    mine.logout()
    assert world.service.authenticate_token(got["token"]) is None
    assert world.refused(mine.logout).status in (401, 403, 404)


def test_only_a_token_logs_out(world: World) -> None:
    assert world.refused(world.acme.logout).status == 400


# --- the pages -----------------------------------------------------------------------------


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


async def test_the_cli_starts_a_login_over_http(world: World) -> None:
    async with site(world) as c:
        resp = await c.post(
            "/auth/device/code", json={"scope": "record", "project": "app", "name": "semula CLI"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["verification_url"] == (
            f"http://127.0.0.1:8080/auth/device?code={body['user_code']}"
        )
        assert (
            await c.post("/auth/device/token", json={"device_code": body["device_code"]})
        ).json() == {"status": "pending"}
        bad = await c.post("/auth/device/token", json={"device_code": "nope"})
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "device_code_invalid"
        assert (await c.post("/auth/device/code", json={"scope": "root"})).status_code == 422


async def test_the_approval_page_asks_a_signed_out_person_to_sign_in_first(world: World) -> None:
    started = world.service.device_start()
    async with site(world) as c:
        resp = await c.get("/auth/device", params={"code": started["user_code"]})
        assert resp.status_code == 302 and resp.headers["location"].startswith("/auth/login?")
        assert "next=" in resp.headers["location"]


async def test_the_approval_page_says_what_is_being_approved_and_is_locked_down(
    world: World,
) -> None:
    ada = world.member("ada")
    cookies, csrf = world.session(ada)
    started = world.service.device_start(scope="record", project="app", name="semula CLI on box")
    async with site(world) as c:
        c.cookies.update(cookies)
        page = await c.get("/auth/device", params={"code": started["user_code"]})
        assert page.status_code == 200
        for shown in (started["user_code"], "acme", "record", "app", "semula CLI on box", csrf):
            assert shown in page.text
        assert "Approve" in page.text and "Deny" in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert "no-store" in page.headers["cache-control"]
        gone = await c.get("/auth/device", params={"code": "ZZZZ-ZZZZ"})
        assert gone.status_code == 404 and "ZZZZ" not in gone.text


async def test_a_signed_in_person_approves_in_the_browser_and_the_cli_gets_its_token(
    world: World,
) -> None:
    ada = world.member("ada")
    cookies, csrf = world.session(ada)
    async with site(world) as c:
        start = (await c.post("/auth/device/code", json={"scope": "record"})).json()
        c.cookies.update(cookies)
        done = await c.post(
            "/auth/device", data={"code": start["user_code"], "csrf": csrf, "decision": "approve"}
        )
        assert done.status_code == 200 and "approved" in done.text.lower()
        got = (
            await c.post("/auth/device/token", json={"device_code": start["device_code"]})
        ).json()
        assert got["status"] == "approved" and got["token"].startswith("smla_live_")


async def test_the_decision_needs_the_session_csrf_token_and_its_own_origin(world: World) -> None:
    ada = world.member("ada")
    cookies, csrf = world.session(ada)
    started = world.service.device_start()
    async with site(world) as c:
        c.cookies.update(cookies)
        form = {"code": started["user_code"], "decision": "approve"}
        assert (await c.post("/auth/device", data={**form, "csrf": "wrong"})).status_code == 403
        other = await c.post(
            "/auth/device", data={**form, "csrf": csrf}, headers={"Origin": "https://evil.test"}
        )
        assert other.status_code == 403
        assert world.service.device_poll(started["device_code"]) == {"status": "pending"}
    async with site(world) as anonymous:
        assert (
            await anonymous.post("/auth/device", data={**form, "csrf": csrf})
        ).status_code == 403


async def test_denying_in_the_browser_gives_the_cli_nothing(world: World) -> None:
    ada = world.member("ada")
    cookies, csrf = world.session(ada)
    started = world.service.device_start()
    async with site(world) as c:
        c.cookies.update(cookies)
        done = await c.post(
            "/auth/device", data={"code": started["user_code"], "csrf": csrf, "decision": "deny"}
        )
        assert done.status_code == 200 and "denied" in done.text.lower()
    assert world.service.device_poll(started["device_code"]) == {"status": "denied"}


async def test_a_person_with_no_workspace_is_told_so(world: World) -> None:
    nobody = world.member("zed", workspace="elsewhere")
    cookies, csrf = world.session(nobody)
    started = world.service.device_start()
    async with site(world) as c:
        c.cookies.update(cookies)
        page = await c.get("/auth/device", params={"code": started["user_code"]})
        assert page.status_code == 200 and "Approve" not in page.text and "workspace" in page.text


# --- the command line, end to end ----------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    where = tmp_path / "config"
    monkeypatch.setenv("SEMULA_CONFIG_DIR", str(where))
    for name in ("SEMULA_API_KEY", "SEMULA_URL"):
        monkeypatch.delenv(name, raising=False)
    return where


def hosted(tmp_path: Path) -> tuple[World, object]:
    world = World(tmp_path)
    return world, running_server(
        tmp_path,
        hosted=True,
        secret=SECRET,
        auth_providers={"google": PROVIDER},
        database_url=world.url,
        mode="replay",
    )


def test_login_whoami_a_command_with_no_flags_and_logout(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world, running = hosted(tmp_path)
    ada = world.member("ada")
    cookies, csrf = world.session(ada)
    with running as url:  # type: ignore[attr-defined]
        approved = []

        def person_approves(_seconds: float) -> None:  # the second session: a browser
            if approved:
                return
            pending = [r for r in world.rows() if r["decided_at"] is None]
            assert len(pending) == 1
            resp = httpx.post(
                url + "/auth/device",
                data={"code": pending[0]["user_code"], "csrf": csrf, "decision": "approve"},
                cookies=cookies,
                headers={"Origin": url},
            )
            assert resp.status_code == 200, resp.text
            approved.append(1)

        monkeypatch.setattr("semula.cli._pause", person_approves)
        assert main(["--json", "login", "--url", url, "--project", "app", "--scope", "record"]) == 0
        out, err = capsys.readouterr()
        done = json.loads(out.strip().splitlines()[-1])
        assert (
            done["workspace"] == "acme" and done["project"] == "app" and done["scope"] == "record"
        )
        assert "/auth/device?code=" in err  # the person is told where to go
        credentials = home / "credentials.json"
        secret = next(iter(json.loads(credentials.read_text())["servers"].values()))["token"]
        assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        assert secret.startswith("smla_live_") and secret not in out + err  # never printed

        # no flags and no environment: the stored server and token are used
        assert main(["--json", "whoami"]) == 0
        me = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert (me["workspace_id"], me["scope"], me["project"]) == ("acme", "record", "app")
        assert main(["--json", "pending"]) == 0
        capsys.readouterr()

        assert main(["logout"]) == 0
        assert not credentials.exists() or secret not in credentials.read_text()
        assert world.service.authenticate_token(secret) is None  # revoked on the server too
        assert main(["--url", url, "whoami"]) == 5  # nothing stored, nothing sent
        assert secret not in "".join(capsys.readouterr())


def test_a_denied_login_stores_nothing_and_exits_with_an_error(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world, running = hosted(tmp_path)
    ada = world.member("ada")
    cookies, csrf = world.session(ada)
    with running as url:  # type: ignore[attr-defined]

        def person_denies(_seconds: float) -> None:
            pending = [r for r in world.rows() if r["decided_at"] is None]
            httpx.post(
                url + "/auth/device",
                data={"code": pending[0]["user_code"], "csrf": csrf, "decision": "deny"},
                cookies=cookies,
                headers={"Origin": url},
            )

        monkeypatch.setattr("semula.cli._pause", person_denies)
        assert main(["login", "--url", url]) == 5
        assert "denied" in capsys.readouterr().err.lower()
        assert not (home / "credentials.json").exists()
        assert world.acme.list_tokens() == []


def test_login_gives_up_when_nobody_approves_in_time(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world, running = hosted(tmp_path)
    with running as url:  # type: ignore[attr-defined]
        monkeypatch.setattr("semula.cli._pause", lambda _s: None)
        assert main(["login", "--url", url, "--wait", "0"]) == 1
        assert "not approved" in capsys.readouterr().err.lower()
        assert not (home / "credentials.json").exists()


def test_the_stored_login_is_kept_per_server_and_forgotten_on_logout(home: Path) -> None:
    from semula import credentials

    credentials.save("http://h.test", {"token": "smla_live_" + "a" * 32})
    assert credentials.token_for("http://h.test/") == "smla_live_" + "a" * 32
    assert credentials.token_for("http://other.test") is None
    assert credentials.default_url() == "http://h.test"
    credentials.forget("http://h.test")
    assert credentials.token_for("http://h.test") is None and credentials.default_url() is None
