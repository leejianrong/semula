"""Membership and invitations: who belongs to a workspace, in which role (ADR-0010 section 3).

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "membership",
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("person_id", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("invited_by", sa.Text),
        sa.PrimaryKeyConstraint("workspace_id", "person_id", name="pk_membership"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], ondelete="CASCADE", name="fk_membership_workspace"
        ),
        sa.ForeignKeyConstraint(
            ["person_id"], ["person.id"], ondelete="CASCADE", name="fk_membership_person"
        ),
    )
    op.create_index("idx_membership_person", "membership", ["person_id"])
    op.create_table(
        "invitation",
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column("invited_by", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Text, nullable=False),
        sa.Column("accepted_at", sa.Text),
        sa.Column("accepted_by", sa.Text),
        sa.Column("revoked_at", sa.Text),
        sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_invitation"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], ondelete="CASCADE", name="fk_invitation_workspace"
        ),
    )
    op.create_index("uq_invitation_hash", "invitation", ["token_hash"], unique=True)
    # Neither table has a row-level-security policy: an invitation is found by its token and
    # memberships are read by person (to find a person's workspaces) before a workspace is
    # chosen, like an API token. The service filters both by workspace for everything else.


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
