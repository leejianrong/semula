"""One place that decides which database a test runs against (ADR-0009).

By default every test gets its own SQLite file. Set ``SEMULA_TEST_DATABASE_URL`` to a
Postgres URL to run the same tests on Postgres; each test then gets a private schema,
so tests stay isolated and can run in any order.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import sqlalchemy as sa

_created: list[tuple[str, str]] = []


def fresh_database_url(tmp_path: Path) -> str:
    base = os.environ.get("SEMULA_TEST_DATABASE_URL")
    if not base:
        return f"sqlite:///{tmp_path / 'semula.db'}"
    from semula.db import normalise_url

    url = normalise_url(base)
    schema = f"sem_{uuid.uuid4().hex[:12]}"
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine.dispose()
    _created.append((url, schema))
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}options=-csearch_path%3D{schema}"


def drop_created_schemas() -> None:
    for url, schema in _created:
        engine = sa.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
    _created.clear()
