"""At most one open pending request per fingerprint.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the earliest of any duplicate open requests so the unique index can be built.
    op.execute(
        "DELETE FROM pending WHERE status = 'pending' AND id NOT IN ("
        "SELECT MIN(id) FROM pending WHERE status = 'pending' "
        "GROUP BY workspace_id, project_id, fingerprint)"
    )
    op.create_index(
        "uq_pending_open",
        "pending",
        ["workspace_id", "project_id", "fingerprint"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_pending_open", table_name="pending")
