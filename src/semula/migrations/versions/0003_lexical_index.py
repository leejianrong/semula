"""A full-text index over recorded requests, for the BM25 tier (SQLite only).

Revision ID: 0003
Revises: 0002

The table is a virtual table, so it is not in ``schema.py``; the service fills it, and
fills any gap at startup. If this SQLite build has no FTS5, the table is skipped and the
lexical tier falls back to a scan (``semula.lexical.ScanIndex``).
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    try:
        op.execute(
            "CREATE VIRTUAL TABLE entry_fts USING fts5("
            "text, entry_id UNINDEXED, workspace_id UNINDEXED, project_id UNINDEXED, "
            "tokenize=\"unicode61 tokenchars '_'\")"
        )
    except sa.exc.OperationalError as exc:
        if "fts5" not in str(exc):
            raise


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TABLE IF EXISTS entry_fts")
