"""What a project has spent forwarding misses upstream (ADR-0013).

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "upstream_usage",
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", name="pk_upstream_usage"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "project_id"],
            ["project.workspace_id", "project.id"],
            ondelete="CASCADE",
            name="fk_upstream_usage_project",
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        setting = "current_setting('semula.workspace_id', true)"
        op.execute("ALTER TABLE upstream_usage ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY semula_tenant ON upstream_usage "
            f"USING (workspace_id = {setting}) WITH CHECK (workspace_id = {setting})"
        )


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
