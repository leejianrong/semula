"""API tokens: scoped, per-project credentials for programs (ADR-0010).

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_token",
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("prefix", sa.Text, nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Text),
        sa.Column("revoked_at", sa.Text),
        sa.Column("last_used_at", sa.Text),
        sa.Column("last_used_from", sa.Text),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_api_token"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], ondelete="CASCADE", name="fk_api_token_workspace"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "project_id"],
            ["project.workspace_id", "project.id"],
            ondelete="CASCADE",
            name="fk_api_token_project",
        ),
    )
    op.create_index("uq_api_token_hash", "api_token", ["token_hash"], unique=True)


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
