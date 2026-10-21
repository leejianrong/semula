"""Index alias.entry_id: the entries list counts aliases per row, and deletes cascade here.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_alias_entry", "alias", ["entry_id"])


def downgrade() -> None:
    op.drop_index("idx_alias_entry", table_name="alias")
