"""Alembic environment. Migrations run on a connection the application hands in."""

from __future__ import annotations

from alembic import context

from semula.schema import metadata

config = context.config
connection = config.attributes.get("connection")
if connection is None:  # pragma: no cover - the app always supplies one
    raise RuntimeError("Run migrations through semula.db.Database, which supplies a connection.")

context.configure(
    connection=connection,
    target_metadata=metadata,
    compare_type=True,
    render_as_batch=connection.dialect.name == "sqlite",
)
with context.begin_transaction():
    context.run_migrations()
