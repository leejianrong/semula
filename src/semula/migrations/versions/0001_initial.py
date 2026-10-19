"""Initial schema: workspace, project, entry, pending, call_log.

This equals the schema the pre-Alembic runner produced at its version 2, so databases
made by that runner are stamped here instead of re-created (see semula.db).

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["workspace_id", "project_id"],
        ["project.workspace_id", "project.id"],
        ondelete="CASCADE",
        name=f"fk_{table}_project",
    )


def upgrade() -> None:
    op.create_table(
        "workspace",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_table(
        "project",
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("settings", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_project"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], ondelete="CASCADE", name="fk_project_workspace"
        ),
    )
    op.create_table(
        "entry",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("exact_fingerprint", sa.Text, nullable=False),
        sa.Column("canonical_request", sa.Text, nullable=False),
        sa.Column("responses", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("hit_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("selection", sa.Text, nullable=False, server_default="first"),
        _fk("entry"),
    )
    op.create_index("idx_entry_fingerprint", "entry", ["workspace_id", "project_id", "fingerprint"])
    op.create_index("idx_entry_exact", "entry", ["workspace_id", "project_id", "exact_fingerprint"])
    op.create_table(
        "pending",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("exact_fingerprint", sa.Text, nullable=False),
        sa.Column("canonical_request", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("entry_id", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("answered_at", sa.Text),
        _fk("pending"),
    )
    op.create_index("idx_pending_status", "pending", ["workspace_id", "project_id", "status"])
    op.create_table(
        "call_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("ts", sa.Text, nullable=False),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("tier", sa.Text),
        sa.Column("score", sa.Float),
        sa.Column("latency_ms", sa.Float),
        sa.Column("entry_id", sa.Text),
        sa.Column("pending_id", sa.Text),
    )
    op.create_index("idx_call_log_ts", "call_log", ["workspace_id", "project_id", "ts"])

    # The one default workspace and project that exist locally (ADR-0004).
    workspace = sa.table("workspace", sa.column("id"), sa.column("name"), sa.column("created_at"))
    project = sa.table(
        "project",
        sa.column("workspace_id"),
        sa.column("id"),
        sa.column("name"),
        sa.column("created_at"),
    )
    from semula.schema import now_iso

    created = now_iso()
    op.bulk_insert(workspace, [{"id": "default", "name": "Default", "created_at": created}])
    op.bulk_insert(
        project,
        [{"workspace_id": "default", "id": "default", "name": "Default", "created_at": created}],
    )


def downgrade() -> None:
    for table in ("call_log", "pending", "entry", "project", "workspace"):
        op.drop_table(table)
