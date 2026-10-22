"""Postgres row-level security: a forgotten workspace filter returns nothing (ADR-0011).

Every tenant table gets a policy that compares ``workspace_id`` with the transaction setting
``semula.workspace_id``, which ``Database`` sets from the workspace its handle acts for.
A role that owns the tables (the migrating one) or is a superuser bypasses the policies;
the application should connect as a role that is neither, so they bind it. SQLite has no
row-level security and one workspace, so this does nothing there.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "project",
    "entry",
    "entry_revision",
    "pending",
    "candidate",
    "alias",
    "entry_vector",
    "call_log",
)
POLICY = "semula_tenant"
SETTING = "current_setting('semula.workspace_id', true)"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} "
            f"USING (workspace_id = {SETTING}) WITH CHECK (workspace_id = {SETTING})"
        )


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
