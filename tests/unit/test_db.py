from pathlib import Path

import pytest

from semula.db import DEFAULT_PROJECT, DEFAULT_WORKSPACE, MIGRATIONS, Database
from semula.ids import new_ulid

pytestmark = pytest.mark.unit

NO_TENANT = {"schema_version", "workspace", "sqlite_sequence"}


def test_migrations_apply_to_an_empty_file(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    assert db.schema_version() == len(MIGRATIONS)
    assert db.query_one("SELECT 1 FROM schema_version") is not None


def test_migrate_is_idempotent_and_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    db = Database(path)
    db.execute("UPDATE project SET name='kept' WHERE id=?", (DEFAULT_PROJECT,))
    db.close()
    again = Database(path)
    again.migrate()
    assert again.schema_version() == len(MIGRATIONS)
    row = again.query_one("SELECT name FROM project WHERE id=?", (DEFAULT_PROJECT,))
    assert row is not None and row["name"] == "kept"
    assert len(again.query("SELECT * FROM schema_version")) == len(MIGRATIONS)


def test_default_workspace_and_project_are_seeded(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    assert db.query_one("SELECT id FROM workspace")["id"] == DEFAULT_WORKSPACE  # type: ignore[index]
    assert db.query_one("SELECT workspace_id FROM project")["workspace_id"] == DEFAULT_WORKSPACE  # type: ignore[index]


def test_every_table_carries_workspace_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    tables = [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")]
    missing = [
        t
        for t in tables
        if t not in NO_TENANT
        and "workspace_id" not in {c["name"] for c in db.query(f"PRAGMA table_info({t})")}
    ]
    assert missing == []


def test_ulids_are_26_chars_and_sort_by_time() -> None:
    a, b = new_ulid(1_000), new_ulid(2_000)
    assert len(a) == 26 and a < b
