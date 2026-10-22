"""People who sign in: a person, their provider identities, and server-side sessions (ADR-0010).

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "person",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("last_login_at", sa.Text),
    )
    op.create_table(
        "identity",
        sa.Column("issuer", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("person_id", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("issuer", "subject", name="pk_identity"),
        sa.ForeignKeyConstraint(
            ["person_id"], ["person.id"], ondelete="CASCADE", name="fk_identity_person"
        ),
    )
    op.create_index("idx_identity_person", "identity", ["person_id"])
    op.create_table(
        "person_session",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("person_id", sa.Text, nullable=False),
        sa.Column("active_workspace", sa.Text),
        sa.Column("csrf", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("last_seen_at", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Text, nullable=False),
        sa.Column("client", sa.Text),
        sa.ForeignKeyConstraint(
            ["person_id"], ["person.id"], ondelete="CASCADE", name="fk_person_session_person"
        ),
    )
    op.create_index("idx_person_session_person", "person_session", ["person_id"])


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
