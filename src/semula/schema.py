"""The database schema, defined once for every backend (ADR-0009).

SQLAlchemy Core tables only; no ORM. Timestamps are ISO-8601 UTC strings and JSON is
stored as text, so SQLite and Postgres behave the same and queries stay portable.
Every table except ``workspace`` carries ``workspace_id`` (ADR-0004).

Migrations live in ``semula/migrations`` and are the source of truth for upgrades;
this metadata is what the code queries against. A test checks the two agree.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

DEFAULT_WORKSPACE = "default"
DEFAULT_PROJECT = "default"

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = sa.MetaData(naming_convention=NAMING)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ts(name: str) -> sa.Column[str]:
    return sa.Column(name, sa.Text, nullable=False, default=now_iso)


workspace = sa.Table(
    "workspace",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    _ts("created_at"),
)

project = sa.Table(
    "project",
    metadata,
    sa.Column(
        "workspace_id",
        sa.Text,
        sa.ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("settings", sa.Text, nullable=False, default="{}"),
    _ts("created_at"),
)


def _project_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["workspace_id", "project_id"],
        ["project.workspace_id", "project.id"],
        ondelete="CASCADE",
        name=f"fk_{table}_project",
    )


entry = sa.Table(
    "entry",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("exact_fingerprint", sa.Text, nullable=False),
    sa.Column("canonical_request", sa.Text, nullable=False),
    sa.Column("responses", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False, default=1),
    sa.Column("pinned", sa.Boolean, nullable=False, default=False),
    sa.Column("hit_count", sa.Integer, nullable=False, default=0),
    _ts("created_at"),
    _ts("updated_at"),
    sa.Column("selection", sa.Text, nullable=False, default="first"),
    _project_fk("entry"),
    sa.Index("idx_entry_fingerprint", "workspace_id", "project_id", "fingerprint"),
    sa.Index("idx_entry_exact", "workspace_id", "project_id", "exact_fingerprint"),
)

pending = sa.Table(
    "pending",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("exact_fingerprint", sa.Text, nullable=False),
    sa.Column("canonical_request", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, default="pending"),
    sa.Column("entry_id", sa.Text),
    _ts("created_at"),
    sa.Column("answered_at", sa.Text),
    _project_fk("pending"),
    sa.Index("idx_pending_status", "workspace_id", "project_id", "status"),
    # At most one open request per fingerprint, enforced by the database so concurrent
    # misses (and several server replicas) cannot park duplicates.
    sa.Index(
        "uq_pending_open",
        "workspace_id",
        "project_id",
        "fingerprint",
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    ),
)

call_log = sa.Table(
    "call_log",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    _ts("ts"),
    sa.Column("mode", sa.Text, nullable=False),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("tier", sa.Text),
    sa.Column("score", sa.Float),
    sa.Column("latency_ms", sa.Float),
    sa.Column("entry_id", sa.Text),
    sa.Column("pending_id", sa.Text),
    sa.Index("idx_call_log_ts", "workspace_id", "project_id", "ts"),
)

# A weak lexical match held for a human: "this request looks like that entry" (ADR-0003).
# Rows stay after a decision so a rejected pairing is remembered and never queued again.
candidate = sa.Table(
    "candidate",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("entry_id", sa.Text, sa.ForeignKey("entry.id", ondelete="CASCADE"), nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("canonical_request", sa.Text, nullable=False),
    sa.Column("score", sa.Float, nullable=False),
    sa.Column("tier", sa.Text, nullable=False),
    # open | approved | rejected | superseded (the request was answered or approved elsewhere)
    sa.Column("status", sa.Text, nullable=False, default="open"),
    _ts("created_at"),
    sa.Column("decided_at", sa.Text),
    _project_fk("candidate"),
    sa.Index("idx_candidate_status", "workspace_id", "project_id", "status"),
    sa.Index("idx_candidate_pair", "workspace_id", "project_id", "fingerprint", "entry_id"),
)

# An approved candidate: this request fingerprint now serves that entry (tier "alias").
alias = sa.Table(
    "alias",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("entry_id", sa.Text, sa.ForeignKey("entry.id", ondelete="CASCADE"), nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("exact_fingerprint", sa.Text, nullable=False),
    sa.Column("canonical_request", sa.Text, nullable=False),
    _ts("created_at"),
    _project_fk("alias"),
    sa.Index("idx_alias_fingerprint", "workspace_id", "project_id", "fingerprint"),
)
