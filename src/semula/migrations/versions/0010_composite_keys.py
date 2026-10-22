"""Composite keys: an entry, pending request, candidate or alias belongs to its workspace.

Until now these ids were global primary keys, so the same ULID could not exist in two
workspaces, a child row could name another workspace's entry, and importing one bundle into two
workspaces collided on the key (ADR-0011). The keys become ``(workspace_id, id)`` and every child
points at its entry with ``(workspace_id, entry_id)``.

Postgres alters the constraints in place. SQLite cannot, so each table is rebuilt (create, copy,
drop, rename, re-index) with foreign keys off, which ``Database.migrate`` arranges.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.schema import CreateTable

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TABLES = ("entry", "entry_revision", "pending", "candidate", "alias", "entry_vector")


def _project_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["workspace_id", "project_id"],
        ["project.workspace_id", "project.id"],
        ondelete="CASCADE",
        name=f"fk_{table}_project",
    )


def _entry_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["workspace_id", "entry_id"],
        ["entry.workspace_id", "entry.id"],
        ondelete="CASCADE",
        name=f"fk_{table}_entry",
    )


def _new_tables() -> dict[str, sa.Table]:
    """The six tables as they are from this revision on. Frozen here, not imported, so later
    schema changes cannot alter what this migration builds."""
    md = sa.MetaData()
    text = sa.Text
    t: dict[str, sa.Table] = {}
    # Never created here: only so the foreign keys below can name their target columns.
    sa.Table("project", md, sa.Column("workspace_id", text), sa.Column("id", text))
    t["entry"] = sa.Table(
        "entry", md,
        sa.Column("workspace_id", text, nullable=False),
        sa.Column("id", text, nullable=False),
        sa.Column("project_id", text, nullable=False),
        sa.Column("fingerprint", text, nullable=False),
        sa.Column("exact_fingerprint", text, nullable=False),
        sa.Column("canonical_request", text, nullable=False),
        sa.Column("responses", text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("hit_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", text, nullable=False),
        sa.Column("updated_at", text, nullable=False),
        sa.Column("selection", text, nullable=False, server_default="first"),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_entry"),
        _project_fk("entry"),
    )  # fmt: skip
    t["entry_revision"] = sa.Table(
        "entry_revision", md,
        sa.Column("workspace_id", text, nullable=False),
        sa.Column("entry_id", text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("ts", text, nullable=False),
        sa.Column("actor", text, nullable=False),
        sa.Column("summary", text, nullable=False),
        sa.Column("forced", sa.Boolean, nullable=False),
        sa.Column("snapshot", text, nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "entry_id", "version", name="pk_entry_revision"),
        _entry_fk("entry_revision"),
    )  # fmt: skip
    t["pending"] = sa.Table(
        "pending", md,
        sa.Column("workspace_id", text, nullable=False),
        sa.Column("id", text, nullable=False),
        sa.Column("project_id", text, nullable=False),
        sa.Column("fingerprint", text, nullable=False),
        sa.Column("exact_fingerprint", text, nullable=False),
        sa.Column("canonical_request", text, nullable=False),
        sa.Column("status", text, nullable=False, server_default="pending"),
        sa.Column("entry_id", text),
        sa.Column("created_at", text, nullable=False),
        sa.Column("answered_at", text),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_pending"),
        _project_fk("pending"),
    )  # fmt: skip
    t["candidate"] = sa.Table(
        "candidate", md,
        sa.Column("workspace_id", text, nullable=False),
        sa.Column("id", text, nullable=False),
        sa.Column("project_id", text, nullable=False),
        sa.Column("entry_id", text, nullable=False),
        sa.Column("fingerprint", text, nullable=False),
        sa.Column("canonical_request", text, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("tier", text, nullable=False),
        sa.Column("status", text, nullable=False),
        sa.Column("created_at", text, nullable=False),
        sa.Column("decided_at", text),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_candidate"),
        _project_fk("candidate"),
        _entry_fk("candidate"),
    )  # fmt: skip
    t["alias"] = sa.Table(
        "alias", md,
        sa.Column("workspace_id", text, nullable=False),
        sa.Column("id", text, nullable=False),
        sa.Column("project_id", text, nullable=False),
        sa.Column("entry_id", text, nullable=False),
        sa.Column("fingerprint", text, nullable=False),
        sa.Column("exact_fingerprint", text, nullable=False),
        sa.Column("canonical_request", text, nullable=False),
        sa.Column("created_at", text, nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_alias"),
        _project_fk("alias"),
        _entry_fk("alias"),
    )  # fmt: skip
    t["entry_vector"] = sa.Table(
        "entry_vector", md,
        sa.Column("workspace_id", text, nullable=False),
        sa.Column("entry_id", text, nullable=False),
        sa.Column("project_id", text, nullable=False),
        sa.Column("model", text, nullable=False),
        sa.Column("vector", sa.LargeBinary, nullable=False),
        sa.Column("created_at", text, nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "entry_id", name="pk_entry_vector"),
        _entry_fk("entry_vector"),
    )  # fmt: skip
    return t


WS_PROJECT = ["workspace_id", "project_id"]
OPEN = "status = 'pending'"
# (name, columns, unique, where) for the indexes each rebuilt table needs again.
INDEXES: dict[str, list[tuple[str, list[str], bool, str | None]]] = {
    "entry": [
        ("idx_entry_fingerprint", [*WS_PROJECT, "fingerprint"], False, None),
        ("idx_entry_exact", [*WS_PROJECT, "exact_fingerprint"], False, None),
    ],
    "pending": [
        ("idx_pending_status", [*WS_PROJECT, "status"], False, None),
        ("uq_pending_open", [*WS_PROJECT, "fingerprint"], True, OPEN),
    ],
    "candidate": [
        ("idx_candidate_status", [*WS_PROJECT, "status"], False, None),
        ("idx_candidate_pair", [*WS_PROJECT, "fingerprint", "entry_id"], False, None),
    ],
    "alias": [
        ("idx_alias_fingerprint", [*WS_PROJECT, "fingerprint"], False, None),
        ("idx_alias_entry", ["workspace_id", "entry_id"], False, None),
    ],
    "entry_vector": [
        ("idx_entry_vector_project", [*WS_PROJECT, "model"], False, None),
    ],
}


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _upgrade_sqlite()
    else:
        _upgrade_postgres()


def _upgrade_sqlite() -> None:
    bind = op.get_bind()
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError(
            "This migration rebuilds tables and needs SQLite foreign keys off; "
            "run it through semula.db.Database."
        )
    tables = _new_tables()
    old_columns = {name: [c["name"] for c in sa.inspect(bind).get_columns(name)] for name in TABLES}
    for name in TABLES:  # create every new table first, then copy, so order cannot matter
        ddl = str(CreateTable(tables[name]).compile(dialect=bind.dialect)).strip()
        op.execute(ddl.replace(f"CREATE TABLE {name} (", f"CREATE TABLE {name}_new (", 1))
    for name in TABLES:
        shared = ", ".join(c for c in old_columns[name])
        op.execute(f"INSERT INTO {name}_new ({shared}) SELECT {shared} FROM {name}")  # noqa: S608
    for name in TABLES:
        op.execute(f"DROP TABLE {name}")
    for name in TABLES:
        op.execute(f"ALTER TABLE {name}_new RENAME TO {name}")
    for name, indexes in INDEXES.items():
        for index, columns, unique, where in indexes:
            op.create_index(
                index,
                name,
                columns,
                unique=unique,
                sqlite_where=sa.text(where) if where else None,
            )
    broken = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise RuntimeError(f"foreign keys do not hold after the rebuild: {broken[:3]}")


def _upgrade_postgres() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # 1. Drop every foreign key that points at entry, and every primary key about to change.
    for child in ("entry_revision", "candidate", "alias", "entry_vector"):
        for fk in inspector.get_foreign_keys(child):
            if fk["referred_table"] == "entry":
                if not fk["name"]:
                    raise RuntimeError(f"{child} has an unnamed foreign key")
                op.drop_constraint(fk["name"], child, type_="foreignkey")
    for name in TABLES:
        pk = inspector.get_pk_constraint(name)
        if not pk["name"]:
            raise RuntimeError(f"{name} has an unnamed primary key")
        op.drop_constraint(pk["name"], name, type_="primary")
    # 2. New primary keys.
    op.create_primary_key("pk_entry", "entry", ["workspace_id", "id"])
    op.create_primary_key("pk_pending", "pending", ["workspace_id", "id"])
    op.create_primary_key("pk_candidate", "candidate", ["workspace_id", "id"])
    op.create_primary_key("pk_alias", "alias", ["workspace_id", "id"])
    op.create_primary_key("pk_entry_vector", "entry_vector", ["workspace_id", "entry_id"])
    op.create_primary_key(
        "pk_entry_revision", "entry_revision", ["workspace_id", "entry_id", "version"]
    )
    # 3. Children name their entry inside their own workspace.
    for child in ("entry_revision", "candidate", "alias", "entry_vector"):
        op.create_foreign_key(
            f"fk_{child}_entry",
            child,
            "entry",
            ["workspace_id", "entry_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )
    op.drop_index("idx_alias_entry", table_name="alias")
    op.create_index("idx_alias_entry", "alias", ["workspace_id", "entry_id"])


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
