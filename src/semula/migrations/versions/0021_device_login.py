"""CLI sign-in by browser approval (SEM-118).

A global table, like ``signup_code``: no workspace column, so no row-level security. Only a hash
of the CLI's secret code is stored.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_login",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("user_code", sa.Text, nullable=False),
        sa.Column("device_hash", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("project", sa.Text),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Text, nullable=False),
        sa.Column("decided_at", sa.Text),
        sa.Column("approved", sa.Boolean),
        sa.Column("person_id", sa.Text),
        sa.Column("workspace_id", sa.Text),
        sa.Column("granted_scope", sa.Text),
        sa.Column("collected_at", sa.Text),
    )
    op.create_index("uq_device_login_user_code", "device_login", ["user_code"], unique=True)
    op.create_index("uq_device_login_hash", "device_login", ["device_hash"], unique=True)


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
