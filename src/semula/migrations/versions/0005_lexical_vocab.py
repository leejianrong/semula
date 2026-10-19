"""Term document-frequencies for the full-text index (SQLite only).

Revision ID: 0005
Revises: 0004

``fts5vocab`` is a read-only view of the index's terms. The lexical search uses it to query
only a prompt's rarest words, so a long prompt does not pay for walking the posting list of
every common word in it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    has_fts = bind.execute(
        sa.text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'entry_fts'")
    ).first()
    if has_fts:
        op.execute("CREATE VIRTUAL TABLE entry_fts_vocab USING fts5vocab(entry_fts, 'row')")


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TABLE IF EXISTS entry_fts_vocab")
