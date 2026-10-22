"""API tokens at the service layer (ADR-0010, SEM-83): minting, authenticating, revoking."""

import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from semula import tokens as tok
from semula.config import Settings
from semula.db import Database
from semula.errors import SemulaError
from semula.schema import iso
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration


@pytest.fixture
def service(tmp_path: Path) -> Service:
    svc = Service(Database(fresh_database_url(tmp_path)), Settings(data_dir=tmp_path))
    svc.create_workspace("a")
    svc.create_workspace("b")
    svc.for_scope(Scope("a")).create_project("shop")
    return svc


def a(service: Service) -> Service:
    return service.for_scope(Scope("a"))


def test_a_token_is_shown_once_and_only_its_hash_is_stored(service: Service) -> None:
    made = a(service).create_token("ci", "replay")
    raw = made["token"]
    assert re.fullmatch(r"smla_live_[A-Za-z0-9]{32}", raw)
    assert made["scope"] == "replay" and made["project"] is None and made["prefix"] == raw[:14]
    (listed,) = a(service).list_tokens()
    assert "token" not in listed and listed["id"] == made["id"] and listed["last_used_at"] is None
    stored = service.db.query("SELECT token_hash FROM api_token")[0]["token_hash"]
    assert stored == tok.digest(raw) and raw not in stored


def test_authenticating_gives_the_principal_the_credential_names(service: Service) -> None:
    raw = a(service).create_token("agent", "record", project="shop")["token"]
    principal = service.authenticate_token(raw)
    assert principal is not None
    assert (principal.kind, principal.workspace_id, principal.scope, principal.project) == (
        "token", "a", "record", "shop",
    )  # fmt: skip
    assert principal.actor == "agent"


@pytest.mark.parametrize("bad", ["", "nope", "smla_live_" + "x" * 32, "smla_live_short"])
def test_anything_else_is_not_a_token(service: Service, bad: str) -> None:
    a(service).create_token("real", "admin")
    assert service.authenticate_token(bad) is None


def test_revoking_is_immediate_in_the_process_that_revokes(service: Service) -> None:
    made = a(service).create_token("short-lived", "record")
    assert service.authenticate_token(made["token"]) is not None  # now cached
    a(service).revoke_token(made["id"])
    assert service.authenticate_token(made["token"]) is None
    assert a(service).list_tokens()[0]["revoked_at"] is not None


def test_a_revocation_elsewhere_is_felt_within_the_cache_lifetime(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = a(service).create_token("other replica", "record")
    assert service.authenticate_token(made["token"]) is not None
    service.db.run("UPDATE api_token SET revoked_at = :t", {"t": iso(datetime.now(UTC))})
    assert service.authenticate_token(made["token"]) is not None  # still cached: the stated bound
    clock = time.monotonic() + tok_ttl() + 1
    monkeypatch.setattr("semula.service.time.monotonic", lambda: clock)
    assert service.authenticate_token(made["token"]) is None


def tok_ttl() -> float:
    from semula.service import TOKEN_CACHE_TTL

    return float(TOKEN_CACHE_TTL)


def test_an_expired_token_does_not_authenticate(service: Service) -> None:
    past = iso(datetime.now(UTC) - timedelta(seconds=1))
    made = a(service).create_token("old", "record", expires_at=past)
    assert service.authenticate_token(made["token"]) is None
    soon = iso(datetime.now(UTC) + timedelta(hours=1))
    assert service.authenticate_token(
        a(service).create_token("new", "record", expires_at=soon)["token"]
    )


def test_rotating_mints_a_successor_and_gives_the_old_token_a_grace_period(
    service: Service,
) -> None:
    old = a(service).create_token("deploy", "record", project="shop")
    new = a(service).rotate_token(old["id"], grace_seconds=3600)
    assert new["token"] != old["token"]
    assert (new["name"], new["scope"], new["project"]) == ("deploy", "record", "shop")
    assert service.authenticate_token(old["token"]) is not None  # deployed config keeps working
    assert service.authenticate_token(new["token"]) is not None
    old_row = next(t for t in a(service).list_tokens() if t["id"] == old["id"])
    assert old_row["expires_at"] is not None
    a(service).rotate_token(new["id"], grace_seconds=0)
    assert service.authenticate_token(new["token"]) is None


def test_last_use_is_written_at_most_once_a_minute(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = a(service).create_token("busy", "replay")
    service.authenticate_token(made["token"], client="203.0.113.77")
    first = a(service).list_tokens()[0]
    assert first["last_used_at"] is not None and first["last_used_from"] == "203.0.113.0/24"
    stamp = first["last_used_at"]
    service._tokens.cache.clear()  # force a lookup that would write again
    service.authenticate_token(made["token"], client="203.0.113.77")
    assert a(service).list_tokens()[0]["last_used_at"] == stamp  # within a minute: not rewritten
    clock = time.monotonic() + 61
    monkeypatch.setattr("semula.service.time.monotonic", lambda: clock)
    service._tokens.cache.clear()
    service.authenticate_token(made["token"], client="203.0.113.77")
    assert a(service).list_tokens()[0]["last_used_at"] != stamp


def test_a_token_can_only_be_made_with_a_real_scope_and_project(service: Service) -> None:
    for kwargs, code in (
        ({"scope": "root"}, "invalid_scope"),
        ({"scope": "record", "project": "nope"}, "project_not_found"),
        ({"scope": "record", "name": "  "}, "invalid_name"),
    ):
        args: dict[str, Any] = {"name": "t", **kwargs}
        with pytest.raises(SemulaError) as caught:
            a(service).create_token(**args)
        assert caught.value.code == code


def test_tokens_belong_to_their_workspace(service: Service) -> None:
    mine = a(service).create_token("mine", "admin")
    b = service.for_scope(Scope("b"))
    assert b.list_tokens() == []
    for act in (b.revoke_token, b.rotate_token):
        with pytest.raises(SemulaError) as caught:
            act(mine["id"])
        assert caught.value.status == 404 and caught.value.code == "token_not_found"
    principal = service.authenticate_token(mine["token"])
    assert principal is not None and principal.workspace_id == "a"


def test_the_scopes_are_ordered() -> None:
    assert tok.LEVEL["replay"] < tok.LEVEL["record"] < tok.LEVEL["admin"]
    assert tok.looks_like_a_token(tok.generate()) and tok.generate() != tok.generate()
    assert tok.coarse_address("2001:db8:1234:5678::1") == "2001:db8:1234::/48"
    assert tok.coarse_address("not an address") is None
