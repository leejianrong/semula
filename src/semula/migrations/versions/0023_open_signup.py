"""Open signup (SEM-123): who made a workspace this way, from where and when.

A global table, like ``signup_redemption``: no workspace column, so no row-level security.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signup_open",
        sa.Column("person_id", sa.Text, primary_key=True),
        sa.Column("address", sa.Text, nullable=False),
        sa.Column("granted_workspace", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(
            ["person_id"], ["person.id"], ondelete="CASCADE", name="fk_signup_open_person"
        ),
    )
    op.create_index("idx_signup_open_created", "signup_open", ["created_at"])
    op.create_index("idx_signup_open_address", "signup_open", ["address", "created_at"])


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
