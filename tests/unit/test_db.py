import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from semula.db import (
    DEFAULT_PROJECT,
    DEFAULT_WORKSPACE,
    Database,
    normalise_url,
)
from semula.ids import new_ulid
from semula.schema import metadata
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.unit

HEAD = "0002"  # bump with every new migration

# What the pre-Alembic hand-rolled runner produced at its version 2.
LEGACY_DDL = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE workspace (id TEXT PRIMARY KEY, name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE project (workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    id TEXT NOT NULL, name TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (workspace_id, id));
CREATE TABLE entry (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, project_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, exact_fingerprint TEXT NOT NULL, canonical_request TEXT NOT NULL,
    responses TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, pinned INTEGER NOT NULL DEFAULT 0,
    hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    selection TEXT NOT NULL DEFAULT 'first',
    FOREIGN KEY (workspace_id, project_id) REFERENCES project(workspace_id, id) ON DELETE CASCADE);
CREATE INDEX idx_entry_fingerprint ON entry(workspace_id, project_id, fingerprint);
CREATE INDEX idx_entry_exact ON entry(workspace_id, project_id, exact_fingerprint);
CREATE TABLE pending (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, project_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, exact_fingerprint TEXT NOT NULL, canonical_request TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', entry_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), answered_at TEXT,
    FOREIGN KEY (workspace_id, project_id) REFERENCES project(workspace_id, id) ON DELETE CASCADE);
CREATE INDEX idx_pending_status ON pending(workspace_id, project_id, status);
CREATE TABLE call_log (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL, ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    mode TEXT NOT NULL, outcome TEXT NOT NULL, tier TEXT, score REAL, latency_ms REAL,
    entry_id TEXT, pending_id TEXT);
CREATE INDEX idx_call_log_ts ON call_log(workspace_id, project_id, ts);
"""


def new_db(tmp_path: Path) -> Database:
    return Database(fresh_database_url(tmp_path))


def test_a_fresh_database_migrates_to_head_and_seeds_the_defaults(tmp_path: Path) -> None:
    db = new_db(tmp_path)
    assert db.revision() == HEAD
    assert db.query_one("SELECT id FROM workspace")["id"] == DEFAULT_WORKSPACE  # type: ignore[index]
    row = db.query_one("SELECT workspace_id, id FROM project")
    assert (row["workspace_id"], row["id"]) == (DEFAULT_WORKSPACE, DEFAULT_PROJECT)  # type: ignore[index]


def test_migrate_is_idempotent_and_keeps_data(tmp_path: Path) -> None:
    url = fresh_database_url(tmp_path)
    db = Database(url)
    db.run("UPDATE project SET name='kept' WHERE id=:id", {"id": DEFAULT_PROJECT})
    db.close()
    again = Database(url)
    again.migrate()
    assert again.revision() == HEAD
    assert again.query_one("SELECT name FROM project")["name"] == "kept"  # type: ignore[index]
    assert len(again.query("SELECT * FROM workspace")) == 1


def test_every_table_carries_workspace_id(tmp_path: Path) -> None:
    db = new_db(tmp_path)
    inspector = sa.inspect(db.engine)
    skip = {"alembic_version", "workspace"}
    missing = [
        t
        for t in inspector.get_table_names()
        if t not in skip and "workspace_id" not in {c["name"] for c in inspector.get_columns(t)}
    ]
    assert missing == []


def test_the_migrations_and_the_schema_module_agree(tmp_path: Path) -> None:
    """If this fails, a table definition changed without a migration (or the reverse)."""
    db = new_db(tmp_path)
    with db.engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), metadata
        )
    assert diff == []


def test_url_spellings_are_normalised_to_the_shipped_driver() -> None:
    assert normalise_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_url("postgresql://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalise_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalise_url("sqlite:///x.db") == "sqlite:///x.db"


def test_ulids_are_26_chars_and_sort_by_time() -> None:
    a, b = new_ulid(1_000), new_ulid(2_000)
    assert len(a) == 26 and a < b


# --- SQLite specifics ---------------------------------------------------------------------


def sqlite_only(tmp_path: Path) -> Path:
    if not fresh_database_url(tmp_path).startswith("sqlite"):
        pytest.skip("SQLite-specific behaviour")
    return tmp_path / "legacy.db"


def test_sqlite_connections_use_wal_foreign_keys_and_a_busy_timeout(tmp_path: Path) -> None:
    sqlite_only(tmp_path)
    db = new_db(tmp_path)
    assert db.query_one("PRAGMA journal_mode")["journal_mode"] == "wal"  # type: ignore[index]
    assert db.query_one("PRAGMA foreign_keys")["foreign_keys"] == 1  # type: ignore[index]
    assert db.query_one("PRAGMA busy_timeout")["timeout"] == 5000  # type: ignore[index]


def make_legacy_file(path: Path, version: int = 2) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_DDL)
    conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
    conn.execute("INSERT INTO workspace (id, name) VALUES ('default', 'Default')")
    conn.execute("INSERT INTO project (workspace_id, id, name) VALUES ('default','default','D')")
    conn.execute("INSERT INTO project (workspace_id, id, name) VALUES ('default','mine','Mine')")
    conn.execute(
        "INSERT INTO entry (id, workspace_id, project_id, fingerprint, exact_fingerprint, "
        "canonical_request, responses, pinned) VALUES "
        "('E1','default','mine','f','x','{\"messages\":[],\"tools\":[]}','[]',1)"
    )
    conn.commit()
    conn.close()


def test_a_file_made_by_the_old_runner_upgrades_in_place_with_its_data(tmp_path: Path) -> None:
    path = sqlite_only(tmp_path)
    make_legacy_file(path)
    db = Database(path)
    assert db.revision() == HEAD
    assert "schema_version" not in sa.inspect(db.engine).get_table_names()
    assert [r["id"] for r in db.query("SELECT id FROM project ORDER BY id")] == ["default", "mine"]
    entry = db.query_one("SELECT id, pinned FROM entry")
    assert entry["id"] == "E1" and bool(entry["pinned"]) is True  # type: ignore[index]
    # and it is a normal database afterwards: a second open changes nothing
    Database(path).migrate()
    assert Database(path).revision() == HEAD


def test_a_legacy_file_at_an_unknown_version_is_refused_not_guessed(tmp_path: Path) -> None:
    path = sqlite_only(tmp_path)
    make_legacy_file(path, version=1)
    with pytest.raises(RuntimeError, match="schema version 1"):
        Database(path)


# --- one open request per fingerprint (migration 0002) -------------------------------------

PENDING = (
    "INSERT INTO pending (id, workspace_id, project_id, fingerprint, exact_fingerprint, "
    "canonical_request, status, created_at) VALUES "
    "(:id, 'default', 'default', 'fp', 'x', '{}', :status, '2026-01-01T00:00:00.000Z')"
)


def test_the_database_refuses_two_open_requests_for_one_fingerprint(tmp_path: Path) -> None:
    db = new_db(tmp_path)
    db.run(PENDING, {"id": "A", "status": "pending"})
    with pytest.raises(sa.exc.IntegrityError):
        db.run(PENDING, {"id": "B", "status": "pending"})
    # answered requests do not count, so history can repeat the same fingerprint
    db.run(PENDING, {"id": "C", "status": "answered"})
    db.run(PENDING, {"id": "D", "status": "answered"})
    assert len(db.query("SELECT id FROM pending")) == 3


def test_upgrading_collapses_duplicate_open_requests_to_the_earliest(tmp_path: Path) -> None:
    path = sqlite_only(tmp_path)
    make_legacy_file(path)
    conn = sqlite3.connect(path)
    for pid in ("P2", "P1", "P3"):
        conn.execute(
            "INSERT INTO pending (id, workspace_id, project_id, fingerprint, exact_fingerprint, "
            "canonical_request) VALUES (?, 'default', 'default', 'same', 'x', '{}')",
            (pid,),
        )
    conn.commit()
    conn.close()
    db = Database(path)
    assert [r["id"] for r in db.query("SELECT id FROM pending")] == ["P1"]
