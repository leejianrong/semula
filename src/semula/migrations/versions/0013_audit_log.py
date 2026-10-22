"""The audit log: who did what, kept a year, never edited (ADR-0010 section 4).

A trigger refuses every UPDATE and every DELETE of a row younger than a year, so no surface (and
no bug) can rewrite history; only the retention prune removes old rows. On Postgres the table
also gets the row-level-security policy of the other tenant tables.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("ts", sa.Text, nullable=False),
        sa.Column("principal_kind", sa.Text, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target", sa.Text),
        sa.Column("detail", sa.Text),
        sa.Column("request_id", sa.Text),
        sa.Column("client", sa.Text),
    )
    op.create_index("idx_audit_log_workspace", "audit_log", ["workspace_id", "id"])
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
            "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER audit_log_keep BEFORE DELETE ON audit_log "
            "WHEN OLD.ts > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-365 days') "
            "BEGIN SELECT RAISE(ABORT, 'audit_log rows are kept for a year'); END"
        )
    else:
        op.execute(
            """
            CREATE FUNCTION semula_audit_guard() RETURNS trigger AS $$
            BEGIN
              IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'audit_log is append-only';
              END IF;
              IF OLD.ts > to_char((now() AT TIME ZONE 'utc') - interval '365 days',
                                  'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') THEN
                RAISE EXCEPTION 'audit_log rows are kept for a year';
              END IF;
              RETURN OLD;
            END
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            "CREATE TRIGGER audit_log_guard BEFORE UPDATE OR DELETE ON audit_log "
            "FOR EACH ROW EXECUTE FUNCTION semula_audit_guard()"
        )
        setting = "current_setting('semula.workspace_id', true)"
        op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY semula_tenant ON audit_log "
            f"USING (workspace_id = {setting}) WITH CHECK (workspace_id = {setting})"
        )


def downgrade() -> None:
    raise NotImplementedError("There is no downgrade (docs/guide/upgrade-and-backup.md).")
