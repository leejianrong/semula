"""The review queue: weak lexical candidates, and aliases made by approving them.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _project_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["workspace_id", "project_id"],
        ["project.workspace_id", "project.id"],
        ondelete="CASCADE",
        name=f"fk_{table}_project",
    )


def upgrade() -> None:
    entry_fk = sa.ForeignKey("entry.id", ondelete="CASCADE")
    op.create_table(
        "candidate",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("entry_id", sa.Text, entry_fk, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("canonical_request", sa.Text, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("tier", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("decided_at", sa.Text),
        _project_fk("candidate"),
        sa.PrimaryKeyConstraint("id", name="pk_candidate"),
    )
    op.create_index("idx_candidate_status", "candidate", ["workspace_id", "project_id", "status"])
    op.create_index(
        "idx_candidate_pair",
        "candidate",
        ["workspace_id", "project_id", "fingerprint", "entry_id"],
    )
    op.create_table(
        "alias",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column(
            "entry_id", sa.Text, sa.ForeignKey("entry.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("exact_fingerprint", sa.Text, nullable=False),
        sa.Column("canonical_request", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        _project_fk("alias"),
        sa.PrimaryKeyConstraint("id", name="pk_alias"),
    )
    op.create_index("idx_alias_fingerprint", "alias", ["workspace_id", "project_id", "fingerprint"])


def downgrade() -> None:
    op.drop_table("alias")
    op.drop_table("candidate")
