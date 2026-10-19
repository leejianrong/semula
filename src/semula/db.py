"""Database access: an engine factory, migrations, and a few thin helpers (ADR-0009).

SQLite is the default (a file in the data dir); a Postgres URL works unchanged. The
service layer queries with SQLAlchemy Core expressions from ``semula.schema``. The
raw-SQL helpers (``query``, ``query_one``) exist for tests and diagnostics only.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.pool import StaticPool

from .schema import DEFAULT_PROJECT, DEFAULT_WORKSPACE

__all__ = ["DEFAULT_PROJECT", "DEFAULT_WORKSPACE", "Database", "make_engine", "normalise_url"]

BASELINE_REVISION = "0001"
LEGACY_SCHEMA_VERSION = 2  # the last version the pre-Alembic runner wrote


def normalise_url(url: str) -> str:
    """Accept the usual spellings and pick the drivers we ship (psycopg 3)."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def make_engine(url: str) -> Engine:
    url = normalise_url(url)
    if not url.startswith("sqlite"):
        return sa.create_engine(url, pool_pre_ping=True, future=True)

    parsed = sa.engine.make_url(url)
    in_memory = parsed.database in (None, "", ":memory:")
    if not in_memory and parsed.database:
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    if in_memory:
        kwargs["poolclass"] = StaticPool
    engine = sa.create_engine(url, future=True, **kwargs)

    @sa.event.listens_for(engine, "connect")
    def _on_connect(dbapi: Any, _record: Any) -> None:
        # Take transaction control from pysqlite so BEGIN/COMMIT behave as SQLAlchemy expects.
        dbapi.isolation_level = None
        cursor = dbapi.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        # In WAL mode NORMAL cannot corrupt the file; a power cut may lose the last commits,
        # which is fine for a recording tool and saves an fsync on every call.
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    @sa.event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN")

    return engine


class Database:
    def __init__(self, url: str | Path) -> None:
        raw = str(url)
        self.url = raw if "://" in raw else f"sqlite:///{raw}"
        self.engine = make_engine(self.url)
        self.migrate()

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    # --- migrations -----------------------------------------------------------------
    def migrate(self) -> None:
        """Bring the database to the latest revision, adopting pre-Alembic SQLite files."""
        with self.engine.begin() as conn:
            self._adopt_legacy(conn)
            config = Config()
            config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
            config.attributes["connection"] = conn
            command.upgrade(config, "head")

    def _adopt_legacy(self, conn: Connection) -> None:
        """A file made by the old hand-rolled runner has ``schema_version`` and no
        ``alembic_version``. Its schema equals the baseline revision, so stamp it."""
        tables = set(sa.inspect(conn).get_table_names())
        if "schema_version" not in tables or "alembic_version" in tables:
            return
        version = conn.execute(sa.text("SELECT MAX(version) FROM schema_version")).scalar()
        if version != LEGACY_SCHEMA_VERSION:
            raise RuntimeError(
                f"This database was written by an older semula (schema version {version}); "
                f"upgrade it with a semula that still has the old migrations, or start fresh."
            )
        conn.execute(sa.text("DROP TABLE schema_version"))
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
            {"rev": BASELINE_REVISION},
        )

    def revision(self) -> str | None:
        with self.engine.connect() as conn:
            rev = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        return str(rev) if rev is not None else None

    # --- queries --------------------------------------------------------------------
    @contextmanager
    def begin(self) -> Iterator[Connection]:
        """One transaction: commits on success, rolls back on any exception."""
        with self.engine.begin() as conn:
            yield conn

    def execute(self, stmt: Any, conn: Connection | None = None) -> int:
        """Run one statement (in ``conn``'s transaction, or its own). Returns rowcount."""
        if conn is not None:
            return int(conn.execute(stmt).rowcount)
        with self.engine.begin() as own:
            return int(own.execute(stmt).rowcount)

    def rows(self, stmt: Any, conn: Connection | None = None) -> list[RowMapping]:
        if conn is not None:
            return list(conn.execute(stmt).mappings().all())
        with self.engine.connect() as own:
            return list(own.execute(stmt).mappings().all())

    def one(self, stmt: Any, conn: Connection | None = None) -> RowMapping | None:
        if conn is not None:
            return conn.execute(stmt).mappings().first()
        with self.engine.connect() as own:
            return own.execute(stmt).mappings().first()

    # Raw SQL with :named parameters, for tests and diagnostics (not the service).
    def query(self, sql: str, params: Mapping[str, Any] | None = None) -> list[RowMapping]:
        return self.rows(sa.text(sql).bindparams(**(params or {})))

    def query_one(self, sql: str, params: Mapping[str, Any] | None = None) -> RowMapping | None:
        return self.one(sa.text(sql).bindparams(**(params or {})))

    def run(self, sql: str, params: Mapping[str, Any] | None = None) -> int:
        return self.execute(sa.text(sql).bindparams(**(params or {})))

    def close(self) -> None:
        self.engine.dispose()
