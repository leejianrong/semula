"""The nearest entry on a missed call (SEM-104), for the miss report.

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("call_log", sa.Column("nearest_entry_id", sa.Text))
    op.add_column("call_log", sa.Column("nearest_score", sa.Float))


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
