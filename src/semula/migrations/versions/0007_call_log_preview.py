"""A redacted preview of the prompt on each call-log row.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("call_log", sa.Column("preview", sa.Text))


def downgrade() -> None:
    op.drop_column("call_log", "preview")
