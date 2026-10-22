"""Deleting a workspace: a 30-day grace, then a purge (ADR-0011 section 5, SEM-89)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from semula import canonical
from semula.app import create_app
from semula.config import Settings
from semula.db import Database
from semula.errors import SemulaError
from semula.schema import iso
from semula.scope import Principal, Scope
from semula.service import Service
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration


def body(text: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


@pytest.fixture
def service(tmp_path: Path) -> Service:
    svc = Service(Database(fresh_database_url(tmp_path)), Settings(data_dir=tmp_path))
    for ws in ("a", "b"):
        svc.create_workspace(ws)
        scoped = svc.for_scope(Scope(ws))
        scoped.create_project("shop")
        entry, _ = scoped.upsert_entry("shop", body(f"{ws} prompt"), {"content": ws})
        scoped.record_candidates(canonical.from_openai(body(f"{ws} prompts")), "shop", [])
        scoped.open_pending(canonical.from_openai(body(f"{ws} waits")), "shop")
        scoped.log_call(
            "hit", 0.0, project="shop", entry_id=entry["id"], req=canonical.from_openai(body("x"))
        )
    return svc


def a(service: Service) -> Service:
    return service.for_scope(Scope("a"))


def refused(call: Any, status: int, code: str) -> None:
    with pytest.raises(SemulaError) as caught:
        call()
    assert (caught.value.status, caught.value.code) == (status, code), caught.value.message


def count(service: Service, table: str, workspace: str) -> int:
    row = service.db.query_one(
        f"SELECT COUNT(*) AS n FROM {table} WHERE workspace_id = :w",  # noqa: S608
        {"w": workspace},
    )
    assert row is not None
    return int(row["n"])


def test_deleting_needs_the_workspace_id_typed_back(service: Service) -> None:
    refused(lambda: a(service).delete_workspace("yes"), 422, "confirmation_mismatch")
    refused(lambda: a(service).delete_workspace("b"), 422, "confirmation_mismatch")
    assert a(service).workspace_status()["deleted_at"] is None
    default = service.for_scope(Scope("default"))
    refused(lambda: default.delete_workspace("default"), 409, "default_workspace")


def test_a_token_cannot_delete_or_restore_a_workspace(service: Service) -> None:
    token = Principal(kind="token", id="T", workspace_id="a", scope="admin")
    mine = service.for_scope(Scope("a", token))
    refused(lambda: mine.delete_workspace("a"), 403, "forbidden")
    refused(lambda: mine.restore_workspace(), 403, "forbidden")


def test_a_deleted_workspace_stops_its_tokens_at_once_but_keeps_its_data(service: Service) -> None:
    secret = a(service).create_token("ci", "admin")["token"]
    assert service.authenticate_token(secret) is not None  # cached
    status = a(service).delete_workspace("a")
    assert status["deleted_at"] is not None and status["purge_after"] > status["deleted_at"]
    assert service.authenticate_token(secret) is None  # at once, cache and all
    assert a(service).export_project("shop")["entries"]  # the operator can still export
    assert service.for_scope(Scope("b")).list_entries("shop")["total"] == 1


def test_restoring_within_the_grace_brings_it_back(service: Service) -> None:
    secret = a(service).create_token("ci", "record")["token"]
    a(service).delete_workspace("a")
    assert service.authenticate_token(secret) is None
    status = a(service).restore_workspace()
    assert status["deleted_at"] is None
    assert service.authenticate_token(secret) is not None
    refused(lambda: a(service).restore_workspace(), 409, "workspace_not_deleted")


def test_purging_waits_for_the_grace_then_removes_everything_of_that_workspace(
    service: Service,
) -> None:
    a(service).delete_workspace("a")
    assert service.purge_workspaces() == []  # not yet
    old = iso(datetime.now(UTC) - timedelta(days=31))
    service.db.run("UPDATE workspace SET deleted_at = :t WHERE id = 'a'", {"t": old})
    assert count(service, "entry", "a") == 1
    assert service.purge_workspaces() == ["a"]
    for table in (
        "project", "entry", "entry_revision", "pending", "candidate", "alias", "api_token",
        "call_log", "workspace_usage",
    ):  # fmt: skip
        assert count(service, table, "a") == 0, table
    assert service.db.query("SELECT id FROM workspace WHERE id = 'a'") == []
    # the other workspace is whole
    for table in ("project", "entry", "entry_revision", "pending", "call_log"):
        assert count(service, table, "b") >= 1, table
    assert service.for_scope(Scope("b")).list_entries("shop")["total"] == 1
    # the audit log keeps the fact, with no content
    kept = [r for r in service.db.query("SELECT * FROM audit_log WHERE workspace_id = 'a'")]
    actions = {r["action"] for r in kept}
    assert {"workspace.delete", "workspace.purge"} <= actions
    assert all("prompt" not in str(dict(r)) for r in kept)


def test_the_search_index_of_a_purged_workspace_is_gone_too(service: Service) -> None:
    from semula.lexical import Fts5Index

    index = service.lexical
    a(service).delete_workspace("a")
    service.db.run("UPDATE workspace SET deleted_at = '2000-01-01T00:00:00.000Z' WHERE id = 'a'")
    service.purge_workspaces()
    if isinstance(index, Fts5Index):
        assert index.indexed_ids("a") == set()
        assert len(index.indexed_ids("b")) == 1


async def test_a_deleted_workspaces_token_gets_a_401(tmp_path: Path) -> None:
    app = create_app(
        Settings(data_dir=tmp_path, hosted=True, database_url=fresh_database_url(tmp_path))
    )
    svc: Service = app.state.service
    svc.create_workspace("a")
    secret = svc.for_scope(Scope("a")).create_token("t", "replay")["token"]
    headers = {"Authorization": f"Bearer {secret}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/v1/models", headers=headers)).status_code == 200
        svc.for_scope(Scope("a")).delete_workspace("a")
        assert (await c.get("/v1/models", headers=headers)).status_code == 401
