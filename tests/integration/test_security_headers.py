"""What a browser is told about the pages and responses semula serves itself (SEM-114).

The dashboard (indah) sends a strict policy already; the sign-in and invitation pages, which semula
writes, and the JSON API sent nothing, so the sign-in page could be framed (clickjacking) and a
response could be sniffed or cached.
"""

import base64
import hashlib
import re

import httpx
import pytest

from semula.config import Settings
from semula.db import Database
from semula.oidc import Identity, Provider
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url
from tests.support.server import running_server

pytestmark = pytest.mark.integration

PROVIDER = Provider("google", "oidc", "client", "secret", issuer="https://idp.test")
SECRET = "a-secret-of-at-least-sixteen-bytes"  # noqa: S105


@pytest.fixture(scope="module")
def hosted(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    tmp = tmp_path_factory.mktemp("headers")
    db_url = fresh_database_url(tmp)
    service = Service(Database(db_url), Settings(data_dir=tmp, hosted=True))
    service.create_workspace("acme")
    ada = service.sign_in_identity(Identity("https://idp.test", "ada", "ada@x.test", True, "Ada"))
    acme = service.for_scope(Scope("acme"))
    service.accept_invitation(acme.create_invitation("ada@x.test", "admin")["token"], ada["id"])
    session, _ = service.create_session(ada["id"])
    with running_server(
        tmp,
        hosted=True,
        secret=SECRET,
        auth_providers={"google": PROVIDER},
        database_url=db_url,
        mode="replay",
    ) as url:
        yield url, {"semula_session": session}


def csp(resp: httpx.Response) -> dict[str, str]:
    raw = resp.headers.get("content-security-policy", "")
    return {d.split()[0]: " ".join(d.split()[1:]) for d in (x.strip() for x in raw.split(";")) if d}


def test_the_sign_in_page_cannot_be_framed_and_runs_no_script(hosted) -> None:  # type: ignore[no-untyped-def]
    url, _ = hosted
    resp = httpx.get(url + "/auth/login", timeout=20)
    policy = csp(resp)
    assert policy["default-src"] == "'none'"
    assert policy["frame-ancestors"] == "'none'"
    assert "script-src" not in policy  # default-src 'none' covers it: the page has no script
    assert policy["img-src"] == "data:"  # the empty inline icon, so no request for /favicon.ico
    assert policy["form-action"] == "'self'"
    assert policy["base-uri"] == "'none'"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"


def test_the_pages_one_style_block_is_allowed_by_hash_not_by_unsafe_inline(hosted) -> None:  # type: ignore[no-untyped-def]
    url, _ = hosted
    resp = httpx.get(url + "/auth/login", timeout=20)
    style = re.search(r"<style>(.*?)</style>", resp.text, re.S)
    assert style, "the page lost its style block"
    digest = base64.b64encode(hashlib.sha256(style.group(1).encode()).digest()).decode()
    allowed = csp(resp)["style-src"]
    assert f"'sha256-{digest}'" in allowed and "unsafe-inline" not in allowed


@pytest.mark.parametrize("path", ["/api/v1/projects", "/v1/models", "/health", "/auth/providers"])
def test_a_json_response_is_not_sniffed_or_cached(hosted, path: str) -> None:  # type: ignore[no-untyped-def]
    url, _ = hosted
    resp = httpx.get(url + path, timeout=20)
    assert resp.headers["x-content-type-options"] == "nosniff", path
    assert resp.headers["cache-control"] == "no-store", path


def test_the_hosted_dashboard_keeps_its_policy(hosted) -> None:  # type: ignore[no-untyped-def]
    """Indah sends this; a regression here would be a dependency upgrade changing it."""
    url, cookies = hosted
    resp = httpx.get(url + "/ui/projects/default/log", cookies=cookies, timeout=20)
    policy = csp(resp)
    assert policy["default-src"] == "'none'"
    assert (
        "'unsafe-inline'" not in policy["script-src"]
        and "'unsafe-eval'" not in policy["script-src"]
    )
    assert re.search(r"'nonce-[A-Za-z0-9_-]{16,}'", policy["script-src"])
    assert policy["frame-ancestors"] == "'none'" or resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-store"
    assert "connect-src" in policy and "'self'" in policy["connect-src"]
