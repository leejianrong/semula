"""Invite codes for signing up (SEM-117).

Two global tables, like ``person``: no workspace column, so no row-level security. Only a hash of
a code is stored; ``signup_redemption`` has one row per person.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signup_code",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column("note", sa.Text, nullable=False),
        sa.Column("max_uses", sa.Integer, nullable=False),
        sa.Column("uses", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Text, nullable=False),
        sa.Column("revoked_at", sa.Text),
    )
    op.create_index("uq_signup_code_hash", "signup_code", ["token_hash"], unique=True)
    op.create_table(
        "signup_redemption",
        sa.Column("person_id", sa.Text, primary_key=True),
        sa.Column("code_id", sa.Text, nullable=False),
        sa.Column("granted_workspace", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(
            ["person_id"], ["person.id"], ondelete="CASCADE", name="fk_signup_redemption_person"
        ),
        sa.ForeignKeyConstraint(
            ["code_id"], ["signup_code.id"], ondelete="CASCADE", name="fk_signup_redemption_code"
        ),
    )
    op.create_index("idx_signup_redemption_code", "signup_redemption", ["code_id"])


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
