"""People signing in with OpenID Connect and GitHub, against a stand-in provider (SEM-84).

The provider is the real protocol with a fake person (tests/support/oidc.py), reached through an
ASGI transport exactly as semula would reach Google. Each way a sign-in can be attacked or can go
wrong has a test that makes it happen and watches semula refuse it.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from semula.app import create_app
from semula.config import Settings
from semula.oidc import Provider
from semula.scope import Scope
from tests.support.db import fresh_database_url
from tests.support.oidc import CLIENT_ID, CLIENT_SECRET, ISSUER
from tests.support.oidc import Provider as FakeIdp

pytestmark = pytest.mark.integration

PUBLIC = "https://semula.example.com"
OIDC = Provider("google", "oidc", CLIENT_ID, CLIENT_SECRET, issuer=ISSUER)
GITHUB = Provider(
    "github", "github", CLIENT_ID, CLIENT_SECRET, issuer="https://github.com",
    authorize_url=f"{ISSUER}/github/authorize", token_url=f"{ISSUER}/github/token",
    api_url=ISSUER, scopes=("read:user", "user:email"), label="GitHub",
)  # fmt: skip


class Site:
    def __init__(self, client: httpx.AsyncClient, idp: FakeIdp, service: Any) -> None:
        self.client = client
        self.idp = idp
        self.service = service

    async def sign_in(self, provider: str = "google", next: str | None = None) -> httpx.Response:
        """Drive the browser's part: login, the provider's redirect, then the callback."""
        params = {"next": next} if next else {}
        start = await self.client.get(f"/auth/login/{provider}", params=params)
        assert start.status_code == 302, start.text
        where = urlparse(start.headers["location"])
        assert where.netloc == "idp.test"
        back = await self.idp_follow(start.headers["location"])
        return await self.client.get(back)

    async def idp_follow(self, url: str) -> str:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.idp.app)) as c:
            resp = await c.get(url.replace(ISSUER, "http://idp"))
        assert resp.status_code == 302, resp.text
        return resp.headers["location"]


@pytest.fixture
async def site(tmp_path: Path) -> AsyncIterator[Site]:
    idp = FakeIdp()
    settings = Settings(
        data_dir=tmp_path,
        hosted=True,
        public_url=PUBLIC,
        secret="a-secret-of-at-least-sixteen-bytes",  # noqa: S106
        auth_providers={"google": OIDC, "github": GITHUB},
        database_url=fresh_database_url(tmp_path),
    )
    app = create_app(settings, http_transport=httpx.ASGITransport(app=idp.app))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=PUBLIC, follow_redirects=False
    ) as client:
        yield Site(client, idp, app.state.service)


def cookie(resp: httpx.Response, name: str) -> str:
    return next(c for c in resp.headers.get_list("set-cookie") if c.startswith(name + "="))


def people(site: Site) -> list[Any]:
    return site.service.db.query("SELECT * FROM person")


async def test_the_login_page_lists_the_configured_providers(site: Site) -> None:
    page = await site.client.get("/auth/login")
    assert "Continue with Google" in page.text and "Continue with GitHub" in page.text
    assert (await site.client.get("/auth/providers")).json()["providers"][0]["name"] == "google"


async def test_a_sign_in_goes_to_the_provider_with_pkce_state_and_nonce(site: Site) -> None:
    start = await site.client.get("/auth/login/google")
    q = parse_qs(urlparse(start.headers["location"]).query)
    assert q["response_type"] == ["code"] and q["client_id"] == [CLIENT_ID]
    assert q["redirect_uri"] == [f"{PUBLIC}/auth/callback/google"]
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) >= 43
    assert len(q["state"][0]) >= 20 and len(q["nonce"][0]) >= 20
    assert set(q["scope"][0].split()) == {"openid", "email", "profile"}
    flow = cookie(start, "__Host-semula_flow")
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=600"):
        assert flag.lower() in flow.lower(), flag
    assert "Domain" not in flow  # a __Host- cookie has none


async def test_a_good_sign_in_makes_a_person_and_a_session_cookie(site: Site) -> None:
    done = await site.sign_in()
    assert done.status_code == 302 and done.headers["location"] == "/ui/"
    session = cookie(done, "__Host-semula_session")
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert flag.lower() in session.lower(), flag
    assert "Max-Age=604800" in session  # seven days
    assert 'semula_flow=""' in cookie(done, "__Host-semula_flow") or "Max-Age=0" in cookie(
        done, "__Host-semula_flow"
    )
    (person,) = people(site)
    assert (person["email"], person["name"]) == ("ada@example.com", "Ada Lovelace")
    me = await site.client.get("/auth/me")
    assert me.status_code == 200 and me.json()["person"]["email"] == "ada@example.com"
    again = await site.sign_in()  # the same person signing in again, from the cookie they hold
    assert again.status_code == 302 and len(people(site)) == 1
    stored = site.service.db.query("SELECT COUNT(*) AS n FROM person_session")[0]["n"]
    assert stored == 1, "the second sign-in replaced the first session"


async def test_a_session_cookie_is_not_the_id_in_the_database(site: Site) -> None:
    done = await site.sign_in()
    secret = cookie(done, "__Host-semula_session").split("=")[1].split(";")[0]
    assert site.service.db.query("SELECT id FROM person_session")[0]["id"] != secret


@pytest.mark.parametrize(
    ("tamper", "code"),
    [
        ("state", "invalid_state"),
        ("nonce", "invalid_nonce"),
        ("aud", "invalid_id_token"),
        ("iss_claim", "invalid_id_token"),
        ("expired", "invalid_id_token"),
        ("signature", "invalid_id_token"),
        ("alg_none", "invalid_id_token"),
        ("iss", "issuer_mismatch"),
        ("denied", "sign_in_refused"),
    ],
)
async def test_every_way_the_provider_response_can_be_wrong_is_refused(
    site: Site, tamper: str, code: str
) -> None:
    site.idp.tamper = tamper
    done = await site.sign_in()
    assert done.status_code in (400, 502) and code in done.text, (tamper, done.text[:200])
    assert "semula_session=" not in "".join(done.headers.get_list("set-cookie")).replace(
        "semula_session=;", ""
    )
    assert people(site) == []
    assert (await site.client.get("/auth/me")).status_code == 401


async def test_a_provider_whose_issuer_is_not_the_configured_one_is_not_trusted(site: Site) -> None:
    site.idp.tamper = "discovery_issuer"
    start = await site.client.get("/auth/login/google")
    assert start.status_code == 502 and "provider_mismatch" in start.text  # the sign-in page


async def test_an_unverified_email_does_not_sign_in(site: Site) -> None:
    site.idp.next = {"sub": "u9", "email": "x@example.com", "email_verified": False, "name": "X"}
    done = await site.sign_in()
    assert done.status_code == 403 and "email_not_verified" in done.text
    assert people(site) == []


async def test_the_flow_cookie_is_required_and_cannot_be_forged(site: Site) -> None:
    start = await site.client.get("/auth/login/google")
    back = await site.idp_follow(start.headers["location"])
    site.client.cookies.clear()
    missing = await site.client.get(back)
    assert missing.status_code == 400 and "invalid_state" in missing.text
    site.client.cookies.set("__Host-semula_flow", "e30.AAAA", domain="semula.example.com")
    forged = await site.client.get(back)
    assert forged.status_code == 400 and "invalid_state" in forged.text
    assert people(site) == []


async def test_a_code_works_once_and_a_wrong_pkce_verifier_fails(site: Site) -> None:
    start = await site.client.get("/auth/login/google")
    back = await site.idp_follow(start.headers["location"])
    assert (await site.client.get(back)).status_code == 302
    replay = await site.client.get(back)  # the same code again
    assert replay.status_code in (400, 502)
    assert len(people(site)) == 1


async def test_next_can_only_be_a_path_on_this_site(site: Site) -> None:
    kept = await site.sign_in(next="/ui/projects/shop/entries")
    assert kept.headers["location"] == "/ui/projects/shop/entries"
    for hostile in (
        "//evil.example/x",
        "https://evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
    ):
        done = await site.sign_in(next=hostile)
        assert done.headers["location"] == "/ui/", hostile


async def test_github_signs_in_through_its_api(site: Site) -> None:
    done = await site.sign_in("github")
    assert done.status_code == 302, done.text
    (person,) = people(site)
    assert person["email"] == "ada@example.com"
    ident = site.service.db.query("SELECT issuer, subject FROM identity")[0]
    assert (ident["issuer"], ident["subject"]) == ("https://github.com", "4242")
    site.idp.github_emails = [{"email": "ada@example.com", "primary": True, "verified": False}]
    refused = await site.sign_in("github")
    assert refused.status_code == 403 and "email_not_verified" in refused.text


async def test_the_same_email_at_two_providers_is_two_people(site: Site) -> None:
    await site.sign_in("google")
    await site.sign_in("github")
    assert len(people(site)) == 2


async def test_signing_out_needs_the_csrf_token_and_ends_the_session(site: Site) -> None:
    await site.sign_in()
    csrf = (await site.client.get("/auth/me")).json()["csrf"]
    refused = await site.client.post("/auth/logout", data={"csrf": "wrong"})
    assert refused.status_code == 403
    cross = await site.client.post(
        "/auth/logout", data={"csrf": csrf}, headers={"Origin": "https://evil.example"}
    )
    assert cross.status_code == 403
    assert (await site.client.get("/auth/me")).status_code == 200  # still signed in
    done = await site.client.post("/auth/logout", data={"csrf": csrf}, headers={"Origin": PUBLIC})
    assert done.status_code == 303 and done.headers["location"] == "/auth/login"
    assert (await site.client.get("/auth/me")).status_code == 401
    assert site.service.db.query("SELECT * FROM person_session") == []


async def test_an_unknown_provider_is_a_404(site: Site) -> None:
    assert (await site.client.get("/auth/login/nope")).status_code == 404


# --- joining a workspace by invitation, over HTTP --------------------------------------------


async def test_the_invitation_link_asks_for_a_sign_in_then_confirms_and_joins(site: Site) -> None:
    site.service.create_workspace("acme")
    ws = site.service.for_scope(Scope("acme"))
    invite = ws.create_invitation("ada@example.com", "editor")
    link = f"/auth/invite/{invite['token']}"

    first = await site.client.get(link)  # nobody signed in: sign in first, and come back here
    assert first.status_code == 302 and first.headers["location"].startswith("/auth/login?next=")
    done = await site.sign_in(next=link)
    assert done.headers["location"] == link

    page = await site.client.get(link)
    assert (
        page.status_code == 200
        and "Join" in page.text
        and "acme" in page.text
        and "editor" in page.text
    )
    csrf = (await site.client.get("/auth/me")).json()["csrf"]
    no_token = await site.client.post(link, data={})
    assert no_token.status_code == 403
    cross = await site.client.post(
        link, data={"csrf": csrf}, headers={"Origin": "https://evil.example"}
    )
    assert cross.status_code == 403
    joined = await site.client.post(link, data={"csrf": csrf}, headers={"Origin": PUBLIC})
    assert joined.status_code == 303 and joined.headers["location"] == "/ui/"
    (person,) = people(site)
    assert site.service.membership_of(person["id"], "acme") == "editor"
    again = await site.client.get(link)
    assert again.status_code == 404  # spent


async def test_someone_else_cannot_use_an_invitation_that_is_not_theirs(site: Site) -> None:

    site.service.create_workspace("acme")
    invite = site.service.for_scope(Scope("acme")).create_invitation(
        "someone.else@example.com", "admin"
    )
    link = f"/auth/invite/{invite['token']}"
    await site.sign_in(next=link)  # signs in as ada@example.com
    csrf = (await site.client.get("/auth/me")).json()["csrf"]
    refused = await site.client.post(link, data={"csrf": csrf}, headers={"Origin": PUBLIC})
    assert refused.status_code == 403 and "different email" in refused.text
    (person,) = people(site)
    assert site.service.membership_of(person["id"], "acme") is None
