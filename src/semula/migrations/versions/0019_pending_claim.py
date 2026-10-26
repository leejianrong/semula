"""A lease on a parked call, for claim_next (SEM-95, ADR-0002 amendment).

Two nullable columns on pending; the table already has row-level security (migration 0018's
policy style), which covers new columns.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pending", sa.Column("claimed_by", sa.Text))
    op.add_column("pending", sa.Column("claim_expires_at", sa.Text))


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
