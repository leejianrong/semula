"""Copy a whole deployment from one database into another (SEM-127): SQLite into Postgres, above
all, so a server can launch on a file and move to Postgres later without losing its people,
tokens or data.

The list of tables is read from ``schema.metadata`` and never written out here, so a table added
to the schema is copied without anyone remembering to; ``tests/integration/test_copy_to.py``
fails when a table is not seeded in its fixture. Everything is copied in one target transaction,
ids and token hashes unchanged, so a failure leaves the target as it was and existing tokens and
sessions keep working. Search indexes and quota counters are rebuilt afterwards.

Stop the server that uses the source first: a copy of a database that is being written is not a
snapshot of anything.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from . import schema
from .config import Settings
from .db import Database, DatabaseNeedsMigration, normalise_url

BATCH = 500

# What a freshly migrated database already holds: the default workspace and its project. They are
# replaced by the source's own, so a target that holds only these counts as empty.
SEED: dict[str, tuple[tuple[Any, ...], ...]] = {
    "workspace": (("default",),),
    "workspace_usage": (("default",),),
    "project": (("default", "default"),),
}


class CopyError(RuntimeError):
    """The copy cannot start or cannot be trusted. The target is left as it was."""


class TargetNotEmpty(CopyError):
    """The target already holds data, so a copy into it would mix two deployments."""


class CopyMismatch(CopyError):
    """A table did not arrive whole."""


@dataclass
class CopyResult:
    counts: dict[str, int]  # rows per table, source and target agreeing
    workspaces: int


def tables() -> list[sa.Table]:
    """Every table of the schema, parents before the tables that refer to them."""
    return list(schema.metadata.sorted_tables)


def _shown(url: str) -> str:
    """A database address safe to print: no password."""
    try:
        return sa.engine.make_url(normalise_url(url)).render_as_string(hide_password=True)
    except sa.exc.ArgumentError:
        return "(an invalid database address)"


def _key(row: Any, table: sa.Table) -> tuple[Any, ...]:
    return tuple(row[c.name] for c in table.primary_key)


def _count(conn: Connection, table: sa.Table) -> int:
    return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar() or 0)


def _check_empty(conn: Connection, url: str) -> None:
    """Refuse a target that holds anything beyond what a fresh migration puts there."""
    for table in tables():
        found = conn.execute(sa.select(table)).mappings().all()
        if not found:
            continue
        expected = SEED.get(table.name, ())
        if any(_key(r, table) not in expected for r in found):
            raise TargetNotEmpty(
                f"The target ({_shown(url)}) is not empty: {table.name} has rows. Copy into a "
                "new, empty database (one that has only been migrated)."
            )


def _check_owner(conn: Connection, url: str) -> None:
    """On Postgres the rows go in as the owner of the tables, which row-level security does not
    apply to. The app's own role is subject to it and could not insert them."""
    if conn.dialect.name != "postgresql":
        return
    row = conn.execute(
        sa.text(
            "SELECT r.rolsuper OR r.rolbypassrls AS bypass, "
            "EXISTS (SELECT 1 FROM pg_tables t WHERE t.tablename = 'entry' "
            "AND t.tableowner = current_user AND t.schemaname = current_schema()) AS owns "
            "FROM pg_roles r WHERE r.rolname = current_user"
        )
    ).one()
    if not (row.bypass or row.owns):
        raise CopyError(
            f"Connect to the target ({_shown(url)}) as the role that owns the tables (the one "
            "that migrates), not as the role the server runs as: row-level security would refuse "
            "the rows. Start the server on the app's role after the copy."
        )


def _copy_table(conn: Connection, source: Connection, table: sa.Table) -> int:
    """Copy one table in batches. Returns the rows read."""
    copied = 0
    result = source.execution_options(stream_results=True).execute(sa.select(table))
    for chunk in result.mappings().partitions(BATCH):
        conn.execute(table.insert(), [dict(r) for r in chunk])
        copied += len(chunk)
    return copied


def _reset_sequences(conn: Connection) -> None:
    """Ids were copied, not drawn from the sequence: move each Postgres sequence past them."""
    if conn.dialect.name != "postgresql":
        return
    for table in tables():
        for column in table.primary_key.columns:
            if isinstance(column.type, sa.Integer) and column.autoincrement is not False:
                conn.execute(
                    sa.text(
                        "SELECT setval(pg_get_serial_sequence(:t, :c), "  # noqa: S608
                        f"COALESCE((SELECT MAX({column.name}) FROM {table.name}), 0) + 1, false)"
                    ),
                    {"t": table.name, "c": column.name},
                )


def copy_database(
    source_url: str,
    target_url: str,
    *,
    settings: Settings | None = None,
    log: Callable[[str], None] | None = None,
) -> CopyResult:
    """Copy every table of ``source_url`` into the empty ``target_url`` and return the counts.

    The target is migrated first (so it has the schema, row-level-security policies and all),
    then filled in one transaction and checked table by table; any failure rolls all of it back.
    The source is only read, in one consistent snapshot."""
    say = log or (lambda _msg: None)
    if normalise_url(source_url) == normalise_url(target_url):
        raise CopyError("The source and the target are the same database.")
    try:
        source = Database(source_url, migrate=False)
    except DatabaseNeedsMigration as exc:
        raise CopyError(f"{exc} Run `semula admin migrate` on the source first.") from exc
    except sa.exc.SQLAlchemyError as exc:
        raise CopyError(
            f"Cannot open the source ({_shown(source_url)}): {type(exc).__name__}"
        ) from exc
    try:
        target = Database(target_url)  # migrates: schema, policies, triggers
    except sa.exc.SQLAlchemyError as exc:
        raise CopyError(
            f"Cannot open the target ({_shown(target_url)}): {type(exc).__name__}"
        ) from exc

    counts: dict[str, int] = {}
    with source.engine.connect() as read:
        if source.dialect == "postgresql":
            read = read.execution_options(isolation_level="REPEATABLE READ")  # noqa: PLW2901
        with target.engine.begin() as conn:
            _check_owner(conn, target_url)
            _check_empty(conn, target_url)
            for table in reversed(tables()):  # drop what the migration seeded, children first
                if table.name in SEED:
                    conn.execute(table.delete())
            for table in tables():
                try:
                    counts[table.name] = _copy_table(conn, read, table)
                except sa.exc.DBAPIError as exc:  # a value the target cannot hold, say
                    raise CopyError(  # the class only: the message can quote a row
                        f"{table.name}: the target refused a row ({type(exc.orig).__name__}). "
                        "Nothing was copied."
                    ) from exc
                say(f"{table.name}: {counts[table.name]}")
            for table in tables():  # every table must have arrived whole, or nothing does
                arrived, held = _count(conn, table), _count(read, table)
                if arrived != counts[table.name] or arrived != held:
                    raise CopyMismatch(
                        f"{table.name}: read {counts[table.name]} rows, the target holds "
                        f"{arrived}, the source holds {held}. Nothing was copied."
                    )
            _reset_sequences(conn)
    _rebuild(target, settings)
    return CopyResult(counts, counts.get("workspace", 0))


def _rebuild(target: Database, settings: Settings | None) -> None:
    """What is derived, not stored data: the lexical index and each workspace's counters."""
    from .scope import Scope
    from .service import Service

    service = Service(target, settings or Settings(data_dir=Path(tempfile.gettempdir())))
    service.sync_index()
    for row in target.rows(sa.select(schema.workspace.c.id)):
        service.for_scope(Scope(row["id"])).recount_usage()
