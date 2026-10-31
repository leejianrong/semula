"""One address or one person cannot hold open an unbounded number of dashboard streams (SEM-115)."""

import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from semula.config import Settings
from semula.dashboard.app import dashboard_limits
from semula.db import Database
from semula.oidc import Identity, Provider
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url
from tests.support.server import running_server

pytestmark = pytest.mark.integration

PROVIDER = Provider("google", "oidc", "client", "secret", issuer="https://idp.test")
SECRET = "a-secret-of-at-least-sixteen-bytes"  # noqa: S105
COOKIE = "semula_session"


def seed(tmp_path: Path) -> tuple[str, dict[str, dict[str, str]]]:
    url = fresh_database_url(tmp_path)
    service = Service(Database(url), Settings(data_dir=tmp_path, hosted=True))
    service.create_workspace("acme")
    acme = service.for_scope(Scope("acme"))
    sessions = {}
    for name in ("ada", "bob"):
        who = service.sign_in_identity(
            Identity("https://idp.test", name, f"{name}@x.test", True, name.title())
        )
        service.accept_invitation(
            acme.create_invitation(f"{name}@x.test", "admin")["token"], who["id"]
        )
        secret, _ = service.create_session(who["id"])
        sessions[name] = {COOKIE: secret}
    return url, sessions


def server(tmp_path: Path, **limits: object) -> Iterator[tuple[str, dict[str, dict[str, str]]]]:
    db_url, sessions = seed(tmp_path)
    with running_server(
        tmp_path,
        hosted=True,
        secret=SECRET,
        auth_providers={"google": PROVIDER},
        database_url=db_url,
        mode="replay",
        **limits,
    ) as url:
        yield url, sessions


def test_the_dashboard_gets_limits_from_the_settings(tmp_path: Path) -> None:
    limits = dashboard_limits(Settings(data_dir=tmp_path, ui_streams_per_address=7, ui_sessions=99))
    assert limits.max_streams_per_ip == 7 and limits.max_sessions == 99
    assert set(limits.rate or {}) == {"/api/event", "/api/upload", "/api/stream"}
    off = dashboard_limits(
        Settings(data_dir=tmp_path, ui_streams_per_address=None, ui_sessions=None)
    )
    assert off.max_streams_per_ip is None and off.max_sessions is None


def test_one_address_cannot_hold_open_more_streams_than_the_cap(tmp_path: Path) -> None:
    for url, sessions in server(tmp_path, ui_streams_per_address=2, ui_tabs_per_person=None):
        statuses = []
        with ExitStack() as stack:
            for _ in range(3):
                sid = httpx.post(
                    url + "/ui/api/session", json={}, cookies=sessions["ada"],
                    headers={"Origin": url}, timeout=30,
                ).json()["token"]  # fmt: skip
                resp = stack.enter_context(
                    httpx.stream(
                        "GET",
                        url + "/ui/api/stream",
                        params={"sid": sid, "path": "/"},
                        cookies=sessions["ada"],
                        timeout=30,
                    )  # fmt: skip
                )
                statuses.append(resp.status_code)
        assert statuses == [200, 200, 429]


def open_stream(url: str, cookies: dict[str, str], stack: ExitStack) -> int:
    sid = httpx.post(
        url + "/ui/api/session", json={}, cookies=cookies, headers={"Origin": url}, timeout=30
    ).json()["token"]
    resp = stack.enter_context(
        httpx.stream(
            "GET",
            url + "/ui/api/stream",
            params={"sid": sid, "path": "/"},
            cookies=cookies,
            timeout=30,
        )  # fmt: skip
    )
    return resp.status_code


def test_one_person_cannot_open_more_tabs_than_the_cap(tmp_path: Path) -> None:
    for url, sessions in server(tmp_path, ui_tabs_per_person=2, ui_streams_per_address=None):
        with ExitStack() as stack:
            assert [open_stream(url, sessions["ada"], stack) for _ in range(3)] == [200, 200, 429]
            # someone else, from the same address, is not held back by ada's tabs
            assert open_stream(url, sessions["bob"], stack) == 200


def test_a_closed_tab_gives_its_place_back(tmp_path: Path) -> None:
    for url, sessions in server(tmp_path, ui_tabs_per_person=1, ui_streams_per_address=None):
        with ExitStack() as first:
            assert open_stream(url, sessions["ada"], first) == 200
            with ExitStack() as refused:
                assert open_stream(url, sessions["ada"], refused) == 429
        deadline = time.monotonic() + 20
        while True:  # the server notices the stream has gone, then a new tab is let in
            with ExitStack() as again:
                if open_stream(url, sessions["ada"], again) == 200:
                    break
            assert time.monotonic() < deadline, "a closed tab never gave its place back"
            time.sleep(0.5)


def test_a_person_who_is_not_signed_in_is_answered_by_the_dashboard_not_the_cap(
    tmp_path: Path,
) -> None:
    for url, _ in server(tmp_path, ui_tabs_per_person=1, ui_streams_per_address=None):
        resp = httpx.get(url + "/ui/api/stream", params={"sid": "x", "path": "/"}, timeout=30)
        assert resp.status_code in (401, 403)  # not 429, whatever was open
