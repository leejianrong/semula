"""Embeddings, one vector per entry (optional feature; the table is empty unless it is on).

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entry_vector",
        sa.Column(
            "entry_id", sa.Text, sa.ForeignKey("entry.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("vector", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("entry_id", name="pk_entry_vector"),
    )
    op.create_index(
        "idx_entry_vector_project", "entry_vector", ["workspace_id", "project_id", "model"]
    )


def downgrade() -> None:
    op.drop_table("entry_vector")
