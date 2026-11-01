"""Suspending a workspace (SEM-122): its tokens and sessions are refused on every surface with a
clear reason, its data stays, and resuming gives everything back. Plus the terms page and the
abuse contact."""

import json
from pathlib import Path

import httpx
import pytest

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
from tests.support.ui import Tab

pytestmark = pytest.mark.integration

SECRET = "a-secret-of-at-least-sixteen-bytes"  # noqa: S105
PROVIDER = Provider("google", "oidc", "client", "secret", issuer="https://idp.test")
COOKIE = "semula_session"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.url = fresh_database_url(tmp_path)
        self.settings = Settings(
            data_dir=tmp_path,
            hosted=True,
            secret=SECRET,
            auth_providers={"google": PROVIDER},
            database_url=self.url,
            abuse_contact="abuse@semula.example",
        )
        self.service = Service(Database(self.url), self.settings)
        for ws in ("acme", "rival"):
            self.service.create_workspace(ws)
        self.acme = self.service.for_scope(Scope("acme"))
        self.rival = self.service.for_scope(Scope("rival"))
        self.acme.create_project("shop")
        self.token = self.acme.create_token("agent", "admin")["token"]
        self.rival_token = self.rival.create_token("agent", "admin")["token"]
        self.ada = self.person("ada", "acme")
        self.cookie, _ = self.service.create_session(self.ada)

    def person(self, name: str, workspace: str) -> str:
        who = self.service.sign_in_identity(
            Identity("https://idp.test", name, f"{name}@x.test", True, name.title())
        )
        invite = self.service.for_scope(Scope(workspace)).create_invitation(
            f"{name}@x.test", "admin"
        )
        self.service.accept_invitation(invite["token"], who["id"])
        return str(who["id"])

    def operator(self, workspace: str) -> Service:
        return self.service.for_scope(Scope(workspace))

    def site(self) -> httpx.AsyncClient:
        app = create_app(self.settings)
        app.state.service = self.service
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def refused(call: object, *args: object) -> SemulaError:
    with pytest.raises(SemulaError) as caught:
        call(*args)  # type: ignore[operator]
    return caught.value


# --- the service ---------------------------------------------------------------------------


def test_suspending_stops_tokens_and_sessions_and_resuming_gives_them_back(world: World) -> None:
    assert world.service.authenticate_token(world.token) is not None
    assert world.service.membership_of(world.ada, "acme") == "admin"
    status = world.operator("acme").suspend_workspace("spam")
    assert status["suspended_at"] and status["suspended_reason"] == "spam"
    err = refused(world.service.authenticate_token, world.token)
    assert (err.status, err.code) == (403, "workspace_suspended")
    assert world.service.membership_of(world.ada, "acme") is None
    assert world.service.workspaces_of(world.ada) == []
    assert (
        world.service.principal_for_session(world.service.authenticate_session(world.cookie))
        is None
    )
    held = world.service.suspended_workspaces_of(world.ada)
    assert [w["workspace_id"] for w in held] == ["acme"]
    # everyone else carries on
    assert world.service.authenticate_token(world.rival_token) is not None
    # and nothing was lost
    resumed = world.operator("acme").resume_workspace()
    assert resumed["suspended_at"] is None
    assert world.service.authenticate_token(world.token) is not None
    assert world.service.membership_of(world.ada, "acme") == "admin"
    assert [p["id"] for p in world.acme.list_projects()] == ["default", "shop"]
    actions = [a["action"] for a in world.acme.list_audit()]
    assert "workspace.suspend" in actions and "workspace.resume" in actions


def test_a_token_already_trusted_in_this_process_is_stopped_at_once(world: World) -> None:
    assert world.service.authenticate_token(world.token) is not None  # now cached
    world.operator("acme").suspend_workspace()
    assert refused(world.service.authenticate_token, world.token).code == "workspace_suspended"


def test_only_the_operator_suspends(world: World) -> None:
    ada = Principal("person", world.ada, "acme", role="admin")
    mine = world.service.for_scope(Scope("acme", ada))
    assert refused(mine.suspend_workspace, "mine").status == 403
    agent = world.service.authenticate_token(world.token)
    assert agent is not None
    assert refused(world.service.for_scope(Scope("acme", agent)).suspend_workspace).status == 403
    assert world.service.membership_of(world.ada, "acme") == "admin"  # nothing happened


def test_suspending_twice_or_resuming_a_workspace_that_is_not_suspended_is_refused(
    world: World,
) -> None:
    assert refused(world.operator("acme").resume_workspace).status == 409
    world.operator("acme").suspend_workspace()
    assert refused(world.operator("acme").suspend_workspace).status == 409
    assert refused(world.operator("default").suspend_workspace).status == 409  # the default one


def test_an_invitation_into_a_suspended_workspace_is_not_valid(world: World) -> None:
    invite = world.acme.create_invitation("bob@x.test", "editor")["token"]
    world.operator("acme").suspend_workspace()
    assert refused(world.service.invitation_preview, invite).status == 404


def test_the_reason_is_bounded(world: World) -> None:
    assert refused(world.operator("acme").suspend_workspace, "x" * 500).status == 422


# --- every surface -------------------------------------------------------------------------


async def test_a_suspended_workspace_is_refused_on_every_token_surface(world: World) -> None:
    world.operator("acme").suspend_workspace("spam")
    auth = {"Authorization": f"Bearer {world.token}"}
    async with world.site() as c:
        for method, path in (
            ("GET", "/api/v1/projects"),
            ("POST", "/v1/chat/completions"),
            ("GET", "/v1/models"),
            ("POST", "/mcp"),
        ):
            resp = await c.request(method, path, headers=auth, json={})
            assert resp.status_code == 403, (path, resp.text)
            assert resp.json()["error"]["code"] == "workspace_suspended"
            assert "suspended" in resp.json()["error"]["message"].lower()
        # it is a refusal, not a failed guess: it does not feed the failed-token limiter
        for _ in range(40):
            resp = await c.get("/api/v1/projects", headers=auth)
            assert resp.status_code == 403
        other = await c.get(
            "/api/v1/projects", headers={"Authorization": f"Bearer {world.rival_token}"}
        )
        assert other.status_code == 200
    world.operator("acme").resume_workspace()
    async with world.site() as c:
        ok = await c.get("/api/v1/projects", headers=auth)
        assert ok.status_code == 200


def test_a_suspended_workspaces_person_is_told_why_on_the_dashboard(
    tmp_path: Path,
) -> None:
    from tests.support.server import running_server

    world = World(tmp_path)
    world.operator("acme").suspend_workspace("spam")
    with running_server(
        tmp_path,
        hosted=True,
        secret=SECRET,
        auth_providers={"google": PROVIDER},
        database_url=world.url,
        abuse_contact="abuse@semula.example",
    ) as url:
        tab = Tab(url, "/projects/shop/log", cookies={COOKIE: world.cookie})
        try:
            tab.wait_for(lambda m: m["type"] == "init", 5)
            text = str(list(tab.nodes()))
            assert "suspended" in text.lower() or tab.saw("suspended")
            assert "abuse@semula.example" in text or tab.saw("abuse@semula.example")
            assert "Send answer" not in text
        finally:
            tab.close()


def test_the_cli_suspends_and_resumes(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEMULA_DATABASE_URL", world.url)
    monkeypatch.setenv("SEMULA_DATA_DIR", str(world.tmp))
    assert main(["--json", "admin", "workspace-suspend", "acme", "--reason", "spam"]) == 0
    got = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert got["suspended_at"] and got["suspended_reason"] == "spam"
    assert refused(world.service.authenticate_token, world.token).code == "workspace_suspended"
    assert main(["admin", "workspace-suspend", "acme"]) == 4  # already
    assert main(["--json", "admin", "workspace-resume", "acme"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["suspended_at"] is None
    assert world.service.authenticate_token(world.token) is not None
    assert main(["admin", "workspace-resume", "acme"]) == 4  # not suspended


def test_the_workspace_table_remembers_when_and_why(world: World) -> None:
    world.operator("acme").suspend_workspace("spam")
    import sqlalchemy as sa

    row = world.service.db.for_workspace(None).one(
        sa.select(schema.workspace.c.suspended_at, schema.workspace.c.suspended_reason).where(
            schema.workspace.c.id == "acme"
        )
    )
    assert row is not None and row["suspended_reason"] == "spam" and row["suspended_at"]


# --- the terms page and the abuse contact --------------------------------------------------


async def test_the_terms_page_is_public_locked_down_and_names_the_abuse_contact(
    world: World,
) -> None:
    async with world.site() as c:
        page = await c.get("/terms")
        assert page.status_code == 200
        assert "abuse@semula.example" in page.text and "acceptable use" in page.text.lower()
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert "script-src" not in page.text


async def test_the_sign_in_page_links_the_terms_and_shows_the_abuse_contact(world: World) -> None:
    async with world.site() as c:
        page = await c.get("/auth/login")
        assert 'href="/terms"' in page.text
        assert "mailto:abuse@semula.example" in page.text


async def test_there_is_no_terms_page_on_a_local_server(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, database_url=fresh_database_url(tmp_path)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
    ) as c:
        assert (await c.get("/terms")).status_code == 404


def test_the_abuse_contact_must_be_an_email_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMULA_ABUSE_CONTACT", "abuse@semula.example")
    assert Settings.from_env().abuse_contact == "abuse@semula.example"
    for bad in ('"><script>', "not an address", "a@b"):
        monkeypatch.setenv("SEMULA_ABUSE_CONTACT", bad)
        with pytest.raises(ValueError, match="SEMULA_ABUSE_CONTACT"):
            Settings.from_env()
    monkeypatch.delenv("SEMULA_ABUSE_CONTACT")
    assert Settings.from_env().abuse_contact == ""
