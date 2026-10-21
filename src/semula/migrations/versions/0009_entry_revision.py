"""Entry history: one snapshot per write, so the editor can show versions and compare them.

Entries that already exist get one revision each, their state as it stands.

Revision ID: 0009
Revises: 0008
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "entry_revision",
        sa.Column(
            "entry_id", sa.Text, sa.ForeignKey("entry.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("ts", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("forced", sa.Boolean, nullable=False),
        sa.Column("snapshot", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("entry_id", "version", name="pk_entry_revision"),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, workspace_id, version, updated_at, responses, selection, pinned FROM entry"
        )
    ).mappings()
    backfill = [
        {
            "entry_id": r["id"],
            "workspace_id": r["workspace_id"],
            "version": r["version"],
            "ts": r["updated_at"],
            "actor": "agent",
            "summary": "before history",
            "forced": False,
            "snapshot": json.dumps(
                {
                    "responses": json.loads(r["responses"]),
                    "selection": r["selection"],
                    "pinned": bool(r["pinned"]),
                }
            ),
        }
        for r in rows
    ]
    if backfill:
        op.bulk_insert(table, backfill)


def downgrade() -> None:
    op.drop_table("entry_revision")
