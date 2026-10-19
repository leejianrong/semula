"""Lexical search over recorded requests: the seam behind the BM25 tier (ADR-0003, ADR-0009).

An index answers one question: which entries of a project look most like these words. It
returns candidates; the service scores them with ``similarity`` and decides whether one is
close enough to serve or only worth a human's review. No full-text SQL lives outside this
module.

* ``Fts5Index``: SQLite FTS5, ranked by BM25. The index is a virtual table kept in step by
  the service (migration 0003).
* ``ScanIndex``: any other database. Reads the project's entries and ranks by word overlap.
  A stopgap until hosted Postgres gets a ``tsvector`` implementation; fine for small projects.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import NamedTuple, Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from .canonical import CanonicalRequest, FingerprintConfig, lexical_tokens
from .db import Database
from .schema import entry as E

FTS_TABLE = "entry_fts"
MAX_QUERY_TOKENS = 256
SCAN_LIMIT = 5000


class LexicalHit(NamedTuple):
    entry_id: str
    rank: float  # backend-specific, only meaningful for ordering (best first)


class LexicalIndex(Protocol):
    def upsert(
        self,
        entry_id: str,
        workspace_id: str,
        project_id: str,
        tokens: Sequence[str],
        conn: Connection | None = None,
    ) -> None: ...

    def delete(self, entry_id: str, conn: Connection | None = None) -> None: ...

    def delete_project(
        self, workspace_id: str, project_id: str, conn: Connection | None = None
    ) -> None: ...

    def indexed_ids(self, conn: Connection | None = None) -> set[str] | None:
        """Entry ids currently indexed, or None if this index keeps no state to repair."""
        ...

    def search(
        self,
        workspace_id: str,
        project_id: str,
        tokens: Sequence[str],
        config: FingerprintConfig,
        limit: int,
    ) -> list[LexicalHit]: ...


def similarity(a: Sequence[str], b: Sequence[str]) -> float:
    """Word-overlap score in [0, 1] (Sorensen-Dice over word counts). 1.0 means the same words."""
    if not a or not b:
        return 0.0
    shared = sum((Counter(a) & Counter(b)).values())
    return 2 * shared / (len(a) + len(b))


class Fts5Index:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(
        self,
        entry_id: str,
        workspace_id: str,
        project_id: str,
        tokens: Sequence[str],
        conn: Connection | None = None,
    ) -> None:
        self.delete(entry_id, conn)
        self.db.execute(
            sa.text(
                "INSERT INTO entry_fts (text, entry_id, workspace_id, project_id) "
                "VALUES (:text, :entry_id, :workspace_id, :project_id)"
            ).bindparams(
                text=" ".join(tokens),
                entry_id=entry_id,
                workspace_id=workspace_id,
                project_id=project_id,
            ),
            conn,
        )

    def delete(self, entry_id: str, conn: Connection | None = None) -> None:
        self.db.execute(
            sa.text("DELETE FROM entry_fts WHERE entry_id = :id").bindparams(id=entry_id), conn
        )

    def delete_project(
        self, workspace_id: str, project_id: str, conn: Connection | None = None
    ) -> None:
        self.db.execute(
            sa.text("DELETE FROM entry_fts WHERE workspace_id = :w AND project_id = :p").bindparams(
                w=workspace_id, p=project_id
            ),
            conn,
        )

    def indexed_ids(self, conn: Connection | None = None) -> set[str]:
        rows = self.db.rows(sa.text("SELECT entry_id FROM entry_fts"), conn)
        return {str(r["entry_id"]) for r in rows}

    def search(
        self,
        workspace_id: str,
        project_id: str,
        tokens: Sequence[str],
        config: FingerprintConfig,
        limit: int,
    ) -> list[LexicalHit]:
        words = list(dict.fromkeys(tokens))[:MAX_QUERY_TOKENS]
        if not words:
            return []
        query = " OR ".join(f'"{w}"' for w in words)  # tokens are \w+, so quoting is enough
        rows = self.db.rows(
            sa.text(
                "SELECT entry_id, bm25(entry_fts) AS score FROM entry_fts "
                "WHERE entry_fts MATCH :query AND workspace_id = :w AND project_id = :p "
                "ORDER BY score LIMIT :limit"
            ).bindparams(query=query, w=workspace_id, p=project_id, limit=limit)
        )
        return [LexicalHit(str(r["entry_id"]), float(r["score"])) for r in rows]


class ScanIndex:
    """No stored index: rank the project's entries by word overlap on every search."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(
        self,
        entry_id: str,
        workspace_id: str,
        project_id: str,
        tokens: Sequence[str],
        conn: Connection | None = None,
    ) -> None:
        return None

    def delete(self, entry_id: str, conn: Connection | None = None) -> None:
        return None

    def delete_project(
        self, workspace_id: str, project_id: str, conn: Connection | None = None
    ) -> None:
        return None

    def indexed_ids(self, conn: Connection | None = None) -> set[str] | None:
        return None

    def search(
        self,
        workspace_id: str,
        project_id: str,
        tokens: Sequence[str],
        config: FingerprintConfig,
        limit: int,
    ) -> list[LexicalHit]:
        wanted = set(tokens)
        if not wanted:
            return []
        rows = self.db.rows(
            sa.select(E.c.id, E.c.canonical_request)
            .where(E.c.workspace_id == workspace_id, E.c.project_id == project_id)
            .order_by(E.c.id)
            .limit(SCAN_LIMIT)
        )
        scored: list[LexicalHit] = []
        for row in rows:
            other = lexical_tokens(CanonicalRequest.from_json(row["canonical_request"]), config)
            shared = len(wanted & set(other))
            if shared:
                scored.append(LexicalHit(str(row["id"]), -float(shared)))
        scored.sort(key=lambda h: h.rank)
        return scored[:limit]


def make_index(db: Database) -> LexicalIndex:
    if db.dialect == "sqlite" and FTS_TABLE in sa.inspect(db.engine).get_table_names():
        return Fts5Index(db)
    return ScanIndex(db)
