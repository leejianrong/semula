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

HEAD = "0012"  # bump with every new migration

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
        if t not in skip
        and not t.startswith("entry_fts")  # SQLite's full-text index (virtual + shadow tables)
        and "workspace_id" not in {c["name"] for c in inspector.get_columns(t)}
    ]
    assert missing == []


def test_the_migrations_and_the_schema_module_agree(tmp_path: Path) -> None:
    """If this fails, a table definition changed without a migration (or the reverse)."""
    db = new_db(tmp_path)
    with db.engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(
                conn,
                opts={
                    "compare_type": True,
                    # the FTS5 virtual table (and its shadow tables) is migration-only
                    "include_object": lambda obj, name, type_, *_: (
                        not (type_ == "table" and name is not None and name.startswith("entry_fts"))
                    ),
                },
            ),
            metadata,
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


def test_upgrading_gives_every_existing_entry_a_first_revision(tmp_path: Path) -> None:
    """History arrived after entries existed: the migration snapshots each one as it stands."""
    url = _database_at(tmp_path, "0008")  # as a release before history left it
    engine = sa.create_engine(dbmod_url(url))
    stamp = "2026-10-01T00:00:00.000Z"
    with engine.begin() as conn:
        for n, (pinned, selection) in enumerate([(False, "first"), (True, "cycle")]):
            _insert(
                conn, "entry", id=f"e{n}", workspace_id=DEFAULT_WORKSPACE,
                project_id=DEFAULT_PROJECT, fingerprint="f", exact_fingerprint="e",
                canonical_request="{}", version=3, pinned=pinned, hit_count=0,
                responses='[{"message": {"role": "assistant", "content": "old"}}]',
                selection=selection, updated_at=stamp, created_at=stamp,
            )  # fmt: skip
    engine.dispose()
    db = Database(url)
    assert db.revision() == HEAD
    rows = db.query("SELECT * FROM entry_revision ORDER BY entry_id")
    assert [(r["entry_id"], r["version"], r["actor"], r["summary"]) for r in rows] == [
        ("e0", 3, "agent", "before history"),
        ("e1", 3, "agent", "before history"),
    ]
    assert rows[0]["ts"] == stamp and not rows[0]["forced"]
    import json

    assert json.loads(rows[1]["snapshot"]) == {
        "responses": [{"message": {"role": "assistant", "content": "old"}}],
        "selection": "cycle",
        "pinned": True,
    }


def test_a_database_made_by_a_newer_semula_is_refused_with_a_clear_message(
    tmp_path: Path,
) -> None:
    from semula.db import DatabaseTooNew

    url = fresh_database_url(tmp_path)
    db = Database(url)
    db.migrate()
    with db.engine.begin() as conn:
        conn.execute(sa.text("UPDATE alembic_version SET version_num = '9999'"))
    with pytest.raises(DatabaseTooNew) as refused:
        Database(url).migrate()
    text = str(refused.value)
    assert "9999" in text and "newer" in text and "backup" in text.lower()


def _database_at(tmp_path: Path, revision: str) -> str:
    """A database migrated only as far as ``revision``, as an older release left it."""
    from alembic import command
    from alembic.config import Config

    import semula.db as dbmod

    url = fresh_database_url(tmp_path)
    engine = dbmod.make_engine(url)
    config = Config()
    config.set_main_option("script_location", str(Path(dbmod.__file__).parent / "migrations"))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)
    engine.dispose()
    return url


def _insert(conn: sa.Connection, table: str, **values: object) -> None:
    columns = ", ".join(values)
    binds = ", ".join(f":{c}" for c in values)
    conn.execute(sa.text(f"INSERT INTO {table} ({columns}) VALUES ({binds})"), values)  # noqa: S608


def _count(db: Database, table: str, where: str = "1 = 1") -> int:
    row = db.query_one(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")  # noqa: S608
    assert row is not None
    return int(row["n"])


def test_composite_keys_keep_every_row_and_let_two_workspaces_share_an_id(tmp_path: Path) -> None:
    """0010 changes the primary and foreign keys of six tables: nothing may be lost, children
    must still cascade, and the same id must now fit in a second workspace."""
    url = _database_at(tmp_path, "0009")
    engine = sa.create_engine(dbmod_url(url))
    t = "2026-10-01T00:00:00.000Z"
    where = {"workspace_id": "default", "project_id": "default"}
    request = {"canonical_request": "{}", "created_at": t}
    with engine.begin() as conn:
        _insert(conn, "entry", id="E1", **where, fingerprint="f", exact_fingerprint="x",
                canonical_request="{}", responses="[]", version=4, pinned=True, hit_count=7,
                created_at=t, updated_at=t, selection="cycle")  # fmt: skip
        _insert(conn, "entry_revision", entry_id="E1", workspace_id="default", version=4, ts=t,
                actor="agent", summary="x", forced=False, snapshot="{}")  # fmt: skip
        _insert(conn, "pending", id="P1", **where, fingerprint="g", exact_fingerprint="y",
                status="answered", entry_id="E1", **request)  # fmt: skip
        _insert(conn, "pending", id="P2", **where, fingerprint="h", exact_fingerprint="y",
                status="pending", **request)  # fmt: skip
        _insert(conn, "candidate", id="C1", **where, entry_id="E1", fingerprint="k", score=0.8,
                tier="bm25", status="open", **request)  # fmt: skip
        _insert(conn, "alias", id="A1", **where, entry_id="E1", fingerprint="m",
                exact_fingerprint="n", **request)  # fmt: skip
        _insert(conn, "entry_vector", entry_id="E1", **where, model="m", vector=b"\x00\x01",
                created_at=t)  # fmt: skip
    engine.dispose()

    db = Database(url)  # migrates 0009 to head
    assert db.revision() == HEAD
    tables = ("entry", "entry_revision", "pending", "candidate", "alias", "entry_vector")
    assert [_count(db, name) for name in tables] == [1, 1, 2, 1, 1, 1]
    kept = db.query_one("SELECT version, pinned, hit_count, selection FROM entry")
    assert kept is not None
    assert (kept["version"], bool(kept["pinned"]), kept["hit_count"], kept["selection"]) == (
        4, True, 7, "cycle",
    )  # fmt: skip
    assert _count(db, "pending", "id = 'P2' AND status = 'pending'") == 1

    # the same ids now fit in a second workspace
    other = {"workspace_id": "b", "project_id": "default"}
    with db.engine.begin() as conn:
        _insert(conn, "workspace", id="b", name="b", created_at=t)
        _insert(conn, "project", workspace_id="b", id="default", name="b", settings="{}",
                created_at=t)  # fmt: skip
        _insert(conn, "entry", id="E1", **other, fingerprint="f", exact_fingerprint="x",
                canonical_request="{}", responses="[]", version=1, pinned=False, hit_count=0,
                created_at=t, updated_at=t, selection="first")  # fmt: skip
        _insert(conn, "candidate", id="C1", **other, entry_id="E1", fingerprint="k", score=0.8,
                tier="bm25", status="open", **request)  # fmt: skip

    # a child cannot name an entry that its own workspace does not have
    with pytest.raises(sa.exc.IntegrityError), db.engine.begin() as conn:
        _insert(conn, "alias", id="A9", **other, entry_id="NOPE", fingerprint="m",
                exact_fingerprint="n", **request)  # fmt: skip

    # deleting default's entry cascades to default's children and leaves b's alone
    db.run("DELETE FROM entry WHERE workspace_id = 'default' AND id = 'E1'")
    for child in ("entry_revision", "candidate", "alias", "entry_vector"):
        assert _count(db, child, "workspace_id = 'default'") == 0, child
    assert _count(db, "candidate", "workspace_id = 'b'") == 1
    assert _count(db, "entry", "workspace_id = 'b'") == 1


def dbmod_url(url: str) -> str:
    from semula.db import normalise_url

    return normalise_url(url)
