"""Postgres row-level security: a query that forgets its workspace filter sees nothing of
another workspace's (ADR-0011). Needs a Postgres (``make test-postgres``) and skips on SQLite."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from semula.config import Settings
from semula.db import Database, normalise_url
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("SEMULA_TEST_DATABASE_URL"), reason="row-level security is Postgres only"
    ),
]


class Tenants:
    def __init__(self, owner: Service, app: Service) -> None:
        self.owner = owner  # connects as the owner (the migrating role): policies do not bind it
        self.app = app  # connects as a plain role: they do


@pytest.fixture
def tenants(tmp_path: Path) -> Iterator[Tenants]:
    owner_url = fresh_database_url(tmp_path)
    owner = Service(Database(owner_url), Settings(data_dir=tmp_path))
    role = f"semula_app_{uuid.uuid4().hex[:8]}"
    schema = sa.make_url(normalise_url(owner_url)).query["options"].split("=")[-1]
    admin = sa.create_engine(normalise_url(owner_url), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f"CREATE ROLE {role} LOGIN PASSWORD 'pw' NOSUPERUSER NOBYPASSRLS"))
        conn.execute(sa.text(f'GRANT USAGE ON SCHEMA "{schema}" TO {role}'))
        conn.execute(sa.text(f'GRANT ALL ON ALL TABLES IN SCHEMA "{schema}" TO {role}'))
        conn.execute(sa.text(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA "{schema}" TO {role}'))
    app_url = sa.make_url(normalise_url(owner_url)).set(username=role, password="pw")  # noqa: S106
    app = Service(
        Database(app_url.render_as_string(hide_password=False)), Settings(data_dir=tmp_path)
    )
    for ws in ("a", "b"):
        owner.create_workspace(ws)
        scoped = owner.for_scope(Scope(ws))
        scoped.create_project("shop")
        scoped.upsert_entry(
            "shop",
            {"messages": [{"role": "user", "content": f"{ws}'s own prompt"}]},
            {"content": f"{ws}'s answer"},
        )
    try:
        yield Tenants(owner, app)
    finally:
        app.db.close()
        with admin.connect() as conn:
            conn.execute(sa.text(f"DROP OWNED BY {role}"))
            conn.execute(sa.text(f"DROP ROLE {role}"))
        admin.dispose()


def workspaces_seen(service: Service, table: str = "entry") -> set[str]:
    rows = service.db.query(f"SELECT workspace_id FROM {table}")  # noqa: S608
    return {r["workspace_id"] for r in rows}


def test_a_query_with_no_filter_sees_only_its_own_workspace(tenants: Tenants) -> None:
    assert workspaces_seen(tenants.owner) >= {"a", "b"}, "the owner is not bound, by design"
    a = tenants.app.for_scope(Scope("a"))
    b = tenants.app.for_scope(Scope("b"))
    for table in ("entry", "project", "entry_revision"):
        assert workspaces_seen(a, table) == {"a"}, table
        assert workspaces_seen(b, table) == {"b"}, table


def test_a_handle_that_names_no_workspace_sees_nothing(tenants: Tenants) -> None:
    nobody = tenants.app.db.for_workspace(None)
    assert nobody.query("SELECT * FROM entry") == []
    assert nobody.query("SELECT * FROM project") == []


def test_a_write_cannot_name_another_workspace(tenants: Tenants) -> None:
    a = tenants.app.for_scope(Scope("a"))
    with pytest.raises(sa.exc.DBAPIError):
        a.db.run(
            "INSERT INTO project (workspace_id, id, name, settings, created_at) "
            "VALUES ('b', 'sneaky', 's', '{}', '2026-10-22T00:00:00.000Z')"
        )
    assert [p["id"] for p in tenants.owner.for_scope(Scope("b")).list_projects()] == ["shop"]


def test_the_service_works_as_the_bound_role(tenants: Tenants) -> None:
    a = tenants.app.for_scope(Scope("a"))
    assert [p["id"] for p in a.list_projects()] == ["shop"]
    entry, created = a.upsert_entry(
        "shop", {"messages": [{"role": "user", "content": "a second prompt"}]}, {"content": "x"}
    )
    assert created and a.get_entry(entry["id"])["version"] == 1
    assert a.list_entries("shop")["total"] == 2
    assert tenants.app.for_scope(Scope("b")).list_entries("shop")["total"] == 1
    from semula import canonical

    found = a.lookup(
        canonical.from_openai({"messages": [{"role": "user", "content": "a second prompt"}]}),
        "shop",
    )
    assert found.match is not None
