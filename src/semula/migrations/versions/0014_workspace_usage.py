"""Per-workspace counters for quotas (ADR-0011 section 6), filled from what is stored now.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_usage",
        sa.Column("workspace_id", sa.Text, primary_key=True),
        sa.Column("projects", sa.Integer, nullable=False, server_default="0"),
        sa.Column("entries", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bytes", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.execute(
        "INSERT INTO workspace_usage (workspace_id, projects, entries, bytes) "
        "SELECT w.id, "
        "(SELECT COUNT(*) FROM project p WHERE p.workspace_id = w.id), "
        "(SELECT COUNT(*) FROM entry e WHERE e.workspace_id = w.id), "
        "COALESCE((SELECT SUM(LENGTH(e.canonical_request) + LENGTH(e.responses)) "
        "FROM entry e WHERE e.workspace_id = w.id), 0) "
        "FROM workspace w"
    )
    if op.get_bind().dialect.name == "postgresql":
        setting = "current_setting('semula.workspace_id', true)"
        op.execute("ALTER TABLE workspace_usage ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY semula_tenant ON workspace_usage "
            f"USING (workspace_id = {setting}) WITH CHECK (workspace_id = {setting})"
        )


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
