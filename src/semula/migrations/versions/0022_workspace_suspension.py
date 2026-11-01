"""Suspending a workspace (SEM-122): when and why, set by the operator.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspace", sa.Column("suspended_at", sa.Text))
    op.add_column("workspace", sa.Column("suspended_reason", sa.Text))


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
