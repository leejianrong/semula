"""SQLite storage with numbered migrations (ADR-0004).

Every table except ``schema_version`` and ``workspace`` carries ``workspace_id``.
One default workspace and project exist locally; auth is not built.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

DEFAULT_WORKSPACE = "default"
DEFAULT_PROJECT = "default"

# Append-only. A migration is never edited once released; add a new one.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE workspace (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    );
    CREATE TABLE project (
        workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
        id TEXT NOT NULL,
        name TEXT NOT NULL,
        settings TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (workspace_id, id)
    );
    CREATE TABLE entry (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        exact_fingerprint TEXT NOT NULL,
        canonical_request TEXT NOT NULL,
        responses TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        pinned INTEGER NOT NULL DEFAULT 0,
        hit_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        FOREIGN KEY (workspace_id, project_id) REFERENCES project(workspace_id, id)
            ON DELETE CASCADE
    );
    CREATE INDEX idx_entry_fingerprint ON entry(workspace_id, project_id, fingerprint);
    CREATE INDEX idx_entry_exact ON entry(workspace_id, project_id, exact_fingerprint);
    CREATE TABLE pending (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        exact_fingerprint TEXT NOT NULL,
        canonical_request TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        entry_id TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        answered_at TEXT,
        FOREIGN KEY (workspace_id, project_id) REFERENCES project(workspace_id, id)
            ON DELETE CASCADE
    );
    CREATE INDEX idx_pending_status ON pending(workspace_id, project_id, status);
    CREATE TABLE call_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        mode TEXT NOT NULL,
        outcome TEXT NOT NULL,
        tier TEXT,
        score REAL,
        latency_ms REAL,
        entry_id TEXT,
        pending_id TEXT
    );
    CREATE INDEX idx_call_log_ts ON call_log(workspace_id, project_id, ts);
    """,
    # 2: how an entry picks among several stored responses (ADR-0005, SEM-21).
    "ALTER TABLE entry ADD COLUMN selection TEXT NOT NULL DEFAULT 'first'",
]


class Database:
    """A single SQLite connection guarded by a lock.

    Calls are short and local, so a lock around one connection is simpler than a
    pool. Async handlers call it directly; nothing here awaits.
    """

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] or 0
            for number, sql in enumerate(MIGRATIONS, start=1):
                if number <= current:
                    continue
                self._conn.execute("BEGIN")
                try:
                    for statement in _split(sql):
                        self._conn.execute(statement)
                    self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
                    if number == 1:
                        self._seed()
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    def _seed(self) -> None:
        self._conn.execute(
            "INSERT INTO workspace (id, name) VALUES (?, ?)", (DEFAULT_WORKSPACE, "Default")
        )
        self._conn.execute(
            "INSERT INTO project (workspace_id, id, name) VALUES (?, ?, ?)",
            (DEFAULT_WORKSPACE, DEFAULT_PROJECT, "Default"),
        )

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, tuple(params)).fetchone()
            return row

    def schema_version(self) -> int:
        row = self.query_one("SELECT MAX(version) AS v FROM schema_version")
        return int(row["v"]) if row and row["v"] is not None else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _split(script: str) -> list[str]:
    # Migrations contain no semicolons inside string literals, so a plain split is safe.
    return [s.strip() for s in script.split(";") if s.strip()]
