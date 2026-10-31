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


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def now_iso() -> str:
    return iso(datetime.now(UTC))


def _ts(name: str) -> sa.Column[str]:
    return sa.Column(name, sa.Text, nullable=False, default=now_iso)


workspace = sa.Table(
    "workspace",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    _ts("created_at"),
    sa.Column("deleted_at", sa.Text),  # set when an admin deletes it; purged after the grace
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


def _entry_fk(table: str) -> sa.ForeignKeyConstraint:
    """A child row names its entry within its own workspace: it cannot point at another's."""
    return sa.ForeignKeyConstraint(
        ["workspace_id", "entry_id"],
        ["entry.workspace_id", "entry.id"],
        ondelete="CASCADE",
        name=f"fk_{table}_entry",
    )


entry = sa.Table(
    "entry",
    metadata,
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("id", sa.Text, nullable=False),
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
    sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_entry"),
    _project_fk("entry"),
    sa.Index("idx_entry_fingerprint", "workspace_id", "project_id", "fingerprint"),
    sa.Index("idx_entry_exact", "workspace_id", "project_id", "exact_fingerprint"),
)

# One snapshot per write to an entry, newest ``MAX_REVISIONS`` kept; the entry's own row stays the
# current state. Written in the same transaction as the write; history is never exported.
entry_revision = sa.Table(
    "entry_revision",
    metadata,
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("entry_id", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    _ts("ts"),
    sa.Column("actor", sa.Text, nullable=False),  # agent | human
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("forced", sa.Boolean, nullable=False, default=False),  # an agent overrode a pin
    sa.Column("snapshot", sa.Text, nullable=False),  # {"responses": [...], "selection", "pinned"}
    sa.PrimaryKeyConstraint("workspace_id", "entry_id", "version", name="pk_entry_revision"),
    _entry_fk("entry_revision"),
)

pending = sa.Table(
    "pending",
    metadata,
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("exact_fingerprint", sa.Text, nullable=False),
    sa.Column("canonical_request", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, default="pending"),
    sa.Column("entry_id", sa.Text),
    _ts("created_at"),
    sa.Column("answered_at", sa.Text),
    # A worker's lease on the call (SEM-95): who holds it and until when. Advisory for
    # claim_next only; anyone may still answer. An expired lease is no lease.
    sa.Column("claimed_by", sa.Text),
    sa.Column("claim_expires_at", sa.Text),
    sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_pending"),
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
    sa.Column("preview", sa.Text),  # redacted, at most 120 characters; null on older rows
    sa.Index("idx_call_log_ts", "workspace_id", "project_id", "ts"),
)

# A weak lexical match held for a human: "this request looks like that entry" (ADR-0003).
# Rows stay after a decision so a rejected pairing is remembered and never queued again.
candidate = sa.Table(
    "candidate",
    metadata,
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("entry_id", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("canonical_request", sa.Text, nullable=False),
    sa.Column("score", sa.Float, nullable=False),
    sa.Column("tier", sa.Text, nullable=False),
    # open | approved | rejected | superseded (the request was answered or approved elsewhere)
    sa.Column("status", sa.Text, nullable=False, default="open"),
    _ts("created_at"),
    sa.Column("decided_at", sa.Text),
    sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_candidate"),
    _project_fk("candidate"),
    _entry_fk("candidate"),
    sa.Index("idx_candidate_status", "workspace_id", "project_id", "status"),
    sa.Index("idx_candidate_pair", "workspace_id", "project_id", "fingerprint", "entry_id"),
)

# One embedding per entry (SEM-E8), only when embeddings are switched on. Raw float32 bytes
# keep it portable across SQLite and Postgres; the search matrix is built in memory.
entry_vector = sa.Table(
    "entry_vector",
    metadata,
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("entry_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("vector", sa.LargeBinary, nullable=False),
    _ts("created_at"),
    sa.PrimaryKeyConstraint("workspace_id", "entry_id", name="pk_entry_vector"),
    _entry_fk("entry_vector"),
    sa.Index("idx_entry_vector_project", "workspace_id", "project_id", "model"),
)

# An approved candidate: this request fingerprint now serves that entry (tier "alias").
alias = sa.Table(
    "alias",
    metadata,
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("entry_id", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("exact_fingerprint", sa.Text, nullable=False),
    sa.Column("canonical_request", sa.Text, nullable=False),
    _ts("created_at"),
    sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_alias"),
    _project_fk("alias"),
    _entry_fk("alias"),
    sa.Index("idx_alias_fingerprint", "workspace_id", "project_id", "fingerprint"),
    # the entries list counts aliases per row and filters on them; deleting an entry cascades here
    sa.Index("idx_alias_entry", "workspace_id", "entry_id"),
)

# A credential for a program (ADR-0010): only its SHA-256 is kept. ``project_id`` null means every
# project of the workspace. Looked up by hash before the workspace is known, so unlike the tenant
# tables it has no row-level-security policy; the hash is 190 bits and it is read by that alone.
api_token = sa.Table(
    "api_token",
    metadata,
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),  # replay | record | admin
    sa.Column("prefix", sa.Text, nullable=False),  # shown in lists to tell tokens apart
    sa.Column("token_hash", sa.Text, nullable=False),
    sa.Column("created_by", sa.Text),
    _ts("created_at"),
    sa.Column("expires_at", sa.Text),
    sa.Column("revoked_at", sa.Text),
    sa.Column("last_used_at", sa.Text),
    sa.Column("last_used_from", sa.Text),  # a network, not an address
    sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_api_token"),
    sa.ForeignKeyConstraint(
        ["workspace_id"], ["workspace.id"], ondelete="CASCADE", name="fk_api_token_workspace"
    ),
    sa.ForeignKeyConstraint(
        ["workspace_id", "project_id"],
        ["project.workspace_id", "project.id"],
        ondelete="CASCADE",
        name="fk_api_token_project",
    ),
    sa.Index("uq_api_token_hash", "token_hash", unique=True),
)

# Who did what, kept for a year and never edited (ADR-0010 section 4). It holds no prompt, answer
# or secret: an action, a target id and a few counts. No foreign key: the rows outlive the
# projects and tokens they name, and a workspace's purge leaves them (with no content) behind.
audit_log = sa.Table(
    "audit_log",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("workspace_id", sa.Text, nullable=False),
    _ts("ts"),
    sa.Column("principal_kind", sa.Text, nullable=False),  # local | token | person
    sa.Column("principal_id", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("target", sa.Text),
    sa.Column("detail", sa.Text),  # small JSON: counts and key names, never content
    sa.Column("request_id", sa.Text),
    sa.Column("client", sa.Text),
    sa.Index("idx_audit_log_workspace", "workspace_id", "id"),
)

# What a workspace holds, counted as it changes inside the write's own transaction, so a replay
# hit never runs a count query and the hit path keeps its latency budget (ADR-0011 section 6).
# ``Service.recount_usage`` rebuilds it from the tables if it ever drifts.
workspace_usage = sa.Table(
    "workspace_usage",
    metadata,
    sa.Column("workspace_id", sa.Text, primary_key=True),
    sa.Column("projects", sa.Integer, nullable=False, default=0),
    sa.Column("entries", sa.Integer, nullable=False, default=0),
    sa.Column("bytes", sa.BigInteger, nullable=False, default=0),
)

# What a project has spent forwarding misses upstream (ADR-0013), counted before each call so a
# cap cannot be crossed by concurrent misses. Only a forwarding project has a row.
upstream_usage = sa.Table(
    "upstream_usage",
    metadata,
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("calls", sa.Integer, nullable=False, default=0),
    sa.Column("prompt_tokens", sa.BigInteger, nullable=False, default=0),
    sa.Column("completion_tokens", sa.BigInteger, nullable=False, default=0),
    sa.PrimaryKeyConstraint("workspace_id", "project_id", name="pk_upstream_usage"),
    _project_fk("upstream_usage"),
)

# People who sign in (ADR-0010). Global, not tenant data: one person can belong to many
# workspaces, and reaching a workspace goes through ``membership`` (SEM-85). A person is found by
# their provider identity, (issuer, subject), never by email.
person = sa.Table(
    "person",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column("name", sa.Text, nullable=False, default=""),
    _ts("created_at"),
    sa.Column("last_login_at", sa.Text),
)

identity = sa.Table(
    "identity",
    metadata,
    sa.Column("issuer", sa.Text, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("person_id", sa.Text, nullable=False),
    sa.Column("email", sa.Text, nullable=False),
    _ts("created_at"),
    sa.PrimaryKeyConstraint("issuer", "subject", name="pk_identity"),
    sa.ForeignKeyConstraint(
        ["person_id"], ["person.id"], ondelete="CASCADE", name="fk_identity_person"
    ),
    sa.Index("idx_identity_person", "person_id"),
)

# A sign-in. ``id`` is the SHA-256 of the cookie's secret, so a copy of this table cannot be used
# to impersonate anyone. Server-side rows are revoked at once and are not tied to one replica.
person_session = sa.Table(
    "person_session",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("person_id", sa.Text, nullable=False),
    sa.Column("active_workspace", sa.Text),
    sa.Column("csrf", sa.Text, nullable=False),
    _ts("created_at"),
    sa.Column("last_seen_at", sa.Text, nullable=False),
    sa.Column("expires_at", sa.Text, nullable=False),  # absolute
    sa.Column("client", sa.Text),
    sa.ForeignKeyConstraint(
        ["person_id"], ["person.id"], ondelete="CASCADE", name="fk_person_session_person"
    ),
    sa.Index("idx_person_session_person", "person_id"),
)

# Who belongs to a workspace and in what role (ADR-0010 section 3): viewer, editor or admin,
# applying to every project in it. The last admin cannot leave or be demoted.
membership = sa.Table(
    "membership",
    metadata,
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("person_id", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    _ts("created_at"),
    sa.Column("invited_by", sa.Text),
    sa.PrimaryKeyConstraint("workspace_id", "person_id", name="pk_membership"),
    sa.ForeignKeyConstraint(
        ["workspace_id"], ["workspace.id"], ondelete="CASCADE", name="fk_membership_workspace"
    ),
    sa.ForeignKeyConstraint(
        ["person_id"], ["person.id"], ondelete="CASCADE", name="fk_membership_person"
    ),
    sa.Index("idx_membership_person", "person_id"),
)

# Invite codes for signing up (SEM-117): the operator makes one, a signed-in person with no
# workspace redeems it and gets a workspace of their own. Global, like ``person``. Only a hash of
# the code is kept. ``signup_redemption`` holds one row per person, so a person redeems once.
signup_code = sa.Table(
    "signup_code",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("token_hash", sa.Text, nullable=False),
    sa.Column("note", sa.Text, nullable=False, default=""),
    sa.Column("max_uses", sa.Integer, nullable=False),
    sa.Column("uses", sa.Integer, nullable=False, default=0),
    _ts("created_at"),
    sa.Column("expires_at", sa.Text, nullable=False),
    sa.Column("revoked_at", sa.Text),
    sa.Index("uq_signup_code_hash", "token_hash", unique=True),
)

signup_redemption = sa.Table(
    "signup_redemption",
    metadata,
    sa.Column("person_id", sa.Text, primary_key=True),
    sa.Column("code_id", sa.Text, nullable=False),
    sa.Column("granted_workspace", sa.Text, nullable=False),  # not a key: it may be purged
    _ts("created_at"),
    sa.ForeignKeyConstraint(
        ["person_id"], ["person.id"], ondelete="CASCADE", name="fk_signup_redemption_person"
    ),
    sa.ForeignKeyConstraint(
        ["code_id"], ["signup_code.id"], ondelete="CASCADE", name="fk_signup_redemption_code"
    ),
    sa.Index("idx_signup_redemption_code", "code_id"),
)

# An offer to join, by email and role. Single use, seven days. Only a hash of its token is kept.
# Accepting needs a signed-in person whose *verified* email matches: an invitation is never a way
# to sign in.
invitation = sa.Table(
    "invitation",
    metadata,
    sa.Column("workspace_id", sa.Text, nullable=False),
    sa.Column("id", sa.Text, nullable=False),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("token_hash", sa.Text, nullable=False),
    sa.Column("invited_by", sa.Text),
    _ts("created_at"),
    sa.Column("expires_at", sa.Text, nullable=False),
    sa.Column("accepted_at", sa.Text),
    sa.Column("accepted_by", sa.Text),
    sa.Column("revoked_at", sa.Text),
    sa.PrimaryKeyConstraint("workspace_id", "id", name="pk_invitation"),
    sa.ForeignKeyConstraint(
        ["workspace_id"], ["workspace.id"], ondelete="CASCADE", name="fk_invitation_workspace"
    ),
    sa.Index("uq_invitation_hash", "token_hash", unique=True),
)
