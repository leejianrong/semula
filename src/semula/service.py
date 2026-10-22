"""The service layer: the only code that reads or writes the database (ADR-0005)."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from . import canonical
from . import tokens as tok
from .canonical import (
    CanonicalRequest,
    FingerprintConfig,
    embedding_text,
    exact_fingerprint,
    lexical_tokens,
    normalised_fingerprint,
    preview_text,
    redact_request,
    roles_of,
    shape,
)
from .config import Settings
from .db import Database
from .embeddings import Embedder, Indexer, VectorStore, load_embedder
from .errors import SemulaError
from .events import EventBus
from .exchange import FORMAT
from .ids import new_ulid
from .lexical import make_index, similarity
from .project_settings import FINGERPRINT_FIELDS, ProjectSettings
from .responses import parse_answer, validate_tool_calls
from .schema import DEFAULT_PROJECT, DEFAULT_WORKSPACE, iso, now_iso
from .schema import alias as A
from .schema import api_token as T
from .schema import audit_log as AU
from .schema import call_log as L
from .schema import candidate as C
from .schema import entry as E
from .schema import entry_revision as R
from .schema import pending as Q
from .schema import project as P
from .schema import workspace as WS
from .schema import workspace_usage as U
from .scope import Principal, Scope

log = logging.getLogger("semula.service")

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


SELECTIONS = ("first", "sequence", "cycle")

LEXICAL_POOL = 10  # entries the lexical index proposes before they are scored
MAX_CANDIDATES = 3  # weak matches queued for review per missed request
PRUNE_EVERY = 200  # call-log rows between retention sweeps
SETTINGS_TTL = 1.0  # seconds a project's settings are reused between reads

MAX_REVISIONS = 50  # snapshots kept per entry; the oldest are dropped
ENTRY_SORTS = {"hits": "hit_count", "updated": "updated_at", "created": "created_at"}
# Stored answers are JSON written by json.dumps, so an answer that calls a tool holds exactly this
# text. A prose mention of it is escaped (\"tool_calls\": [) and cannot match.
TOOL_CALLS_LIKE = '%"tool_calls": [%'

STATS_OUTCOMES = {
    "hit": "hits",
    "miss": "misses",
    "recorded": "recorded",
    "record_timeout": "timed_out",
}
_WINDOW = re.compile(r"(\d+)([mhd])")
MAX_WINDOW_MINUTES = 24 * 60

TIERS = ("exact", "normalised", "alias", "words", "meaning")
# What one tier saw, kept only when test_match asks: tier -> (verdict, score, short reason).
Trace = dict[str, tuple[str, float | None, str]]


@dataclass(frozen=True)
class Match:
    entry_id: str
    tier: str
    stored: dict[str, Any]
    score: float = 1.0


@dataclass(frozen=True)
class Candidate:
    entry_id: str
    score: float
    tier: str = "bm25"


@dataclass(frozen=True)
class MatchResult:
    """A served match, or none plus the weak candidates a human might approve."""

    match: Match | None
    candidates: list[Candidate]
    # The most similar recorded entry whatever its score, to explain a miss. Never served.
    nearest: dict[str, Any] | None = None


WORKSPACE_GRACE_DAYS = 30  # a deleted workspace can be restored for this long, then it is purged
AUDIT_RETENTION_DAYS = 365  # the audit log is kept this long, then pruned (never edited)
TOKEN_CACHE_TTL = 30.0  # seconds a verified token is trusted without asking the database
TOKEN_TOUCH_EVERY = 60.0  # a token's last-used time is written at most this often


class _TokenState:
    """What every scoped copy of a service shares about tokens: verified ones (by hash) and
    when each token's last use was last written."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[float, Principal, str | None]] = {}
        self.touched: dict[str, float] = {}


class Service:
    def __init__(self, db: Database, settings: Settings, embedder: Embedder | None = None) -> None:
        self.db = db.for_workspace(DEFAULT_WORKSPACE)
        self.settings = settings
        # Held calls waiting on an answer: pending id -> [(their event loop, their event)].
        # Answers arrive on worker threads, so waking a waiter must go through its loop.
        self._waiters: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}
        self._waiters_lock = threading.Lock()
        # What a project follows until it chooses its own mode. Lives in memory: it starts from
        # SEMULA_MODE and PUT /api/v1/mode moves it, but a restart returns to the environment.
        self.workspace_id = DEFAULT_WORKSPACE  # ``for_scope`` binds a copy to another workspace
        # A project-bound token reaches this project and no other (ADR-0010); None is all.
        self.project_limit: str | None = None
        self.acting_as_token = False
        # Who is acting and where from, for the audit log (ADR-0010 section 4).
        self.principal: Principal | None = None
        self.request_id: str | None = None
        self.client: str | None = None
        self._process: dict[str, str] = {"mode": settings.mode}  # shared by every scoped copy
        self._tokens = _TokenState()
        self.lexical = make_index(self.db)
        # Semantic matching is optional: with no embedder none of this loads (ADR-0003).
        if embedder is None and settings.embeddings:
            embedder = load_embedder(
                settings.resolved_model_dir, offline=settings.embeddings_offline
            )
        self.vectors: VectorStore | None = VectorStore(self.db, embedder) if embedder else None
        self.indexer: Indexer | None = (
            Indexer(self.vectors, self._texts_to_embed, DEFAULT_WORKSPACE) if self.vectors else None
        )
        self.events = EventBus()
        self._settings_cache: dict[tuple[str, str], tuple[float, ProjectSettings]] = {}
        self._logged = 0

    # --- mode -----------------------------------------------------------------------
    @property
    def default_mode(self) -> str:
        """What a project follows until it chooses its own mode (one value per process)."""
        return self._process["mode"]

    @default_mode.setter
    def default_mode(self, mode: str) -> None:
        self._process["mode"] = mode

    def for_scope(self, scope: Scope) -> Service:
        """This service bound to ``scope``'s workspace: same database, caches and event bus,
        every query filtered by that workspace. Meaning-based matching is only built for the
        local workspace so far, so another workspace has it switched off."""
        if scope.workspace_id == self.workspace_id:
            return self
        bound = copy.copy(self)
        bound.workspace_id = scope.workspace_id
        principal = scope.principal
        bound.principal = principal
        bound.request_id = scope.request_id
        bound.client = scope.client
        bound.acting_as_token = principal is not None and principal.kind == "token"
        bound.project_limit = principal.project if bound.acting_as_token and principal else None
        bound.db = self.db.for_workspace(scope.workspace_id)
        bound.lexical = self.lexical.bound(bound.db)
        if scope.workspace_id != DEFAULT_WORKSPACE:
            bound.vectors = None
            bound.indexer = None
        return bound

    def _allowed(self, project: str) -> str:
        """404 for a project outside a bound token's own, exactly as if it did not exist."""
        if self.project_limit is not None and project != self.project_limit:
            raise SemulaError(404, "project_not_found", f"No project '{project}'.", param="project")
        return project

    def _narrow(self, project: str | None) -> str | None:
        """The project a listing is about: the one asked for (which must be allowed), else a bound
        token's own, else None for every project."""
        if project:
            return self._allowed(project)
        return self.project_limit

    def _own(self, project_id: str, code: str, what: str) -> None:
        """A row found by id belongs to a project this caller may see, or it is a 404."""
        if self.project_limit is not None and project_id != self.project_limit:
            raise SemulaError(404, code, what)

    def _not_a_token(self) -> None:
        """Tokens, bound or not, never manage tokens or the operator's settings (ADR-0010)."""
        if self.acting_as_token:
            raise SemulaError(
                403, "forbidden", "Tokens cannot manage tokens.", err_type="authentication_error"
            )

    def _pk(self, table: sa.Table, slug: str) -> tuple[Any, Any]:
        return (table.c.workspace_id == self.workspace_id, table.c.id == slug)

    def _rk(self, entry_id: str) -> tuple[Any, Any]:
        """Revisions of one entry, within this workspace."""
        return (R.c.workspace_id == self.workspace_id, R.c.entry_id == entry_id)

    # --- API tokens (ADR-0010) ---------------------------------------------------------
    def create_token(
        self,
        name: str,
        scope: str,
        *,
        project: str | None = None,
        expires_at: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Mint a token for this workspace. The secret is in the result once and never again."""
        self._not_a_token()
        label = name.strip()
        if not label or len(label) > 80:
            raise SemulaError(
                422, "invalid_name", "A token needs a name of 1 to 80 characters.", param="name"
            )
        if scope not in tok.SCOPES:
            raise SemulaError(
                422, "invalid_scope", f"scope must be one of {list(tok.SCOPES)}.", param="scope"
            )
        if project is not None:
            self.require_project(project)
        cap = self.settings.quotas.tokens
        if cap is not None:
            active = self.usage()["used"]["tokens"]
            if active >= cap:
                raise self._over("tokens", cap, active)
        secret = tok.generate()
        token_id = new_ulid()
        with self.db.begin() as conn:
            self.db.execute(
                sa.insert(T).values(
                    workspace_id=self.workspace_id,
                    id=token_id,
                    project_id=project,
                    name=label,
                    scope=scope,
                    prefix=tok.display_prefix(secret),
                    token_hash=tok.digest(secret),
                    created_by=created_by,
                    expires_at=expires_at,
                ),
                conn,
            )
            self._audit(
                "token.create", token_id, {"scope": scope, "project": project, "name": label}, conn
            )
        return {**self._token_view(self._token_row(token_id)), "token": secret}

    def list_tokens(self) -> list[dict[str, Any]]:
        self._not_a_token()
        rows = self.db.rows(
            sa.select(T)
            .where(T.c.workspace_id == self.workspace_id)
            .order_by(T.c.created_at, T.c.id)
        )
        return [self._token_view(r) for r in rows]

    def revoke_token(self, token_id: str) -> dict[str, Any]:
        """Stop a token working. In this process the effect is immediate; another replica that
        has the token cached notices within ``TOKEN_CACHE_TTL`` seconds."""
        self._not_a_token()
        self._token_row(token_id)
        with self.db.begin() as conn:
            self.db.execute(
                sa.update(T)
                .where(*self._pk(T, token_id), T.c.revoked_at.is_(None))
                .values(revoked_at=now_iso()),
                conn,
            )
            self._audit("token.revoke", token_id, conn=conn)
        self._forget_token(token_id)
        return self._token_view(self._token_row(token_id))

    def rotate_token(self, token_id: str, grace_seconds: float = 86400.0) -> dict[str, Any]:
        """Mint a successor with the same name, scope and project, and let the old token keep
        working for ``grace_seconds`` so deployed configuration can be swapped without an outage."""
        self._not_a_token()
        old = self._token_row(token_id)
        successor = self.create_token(
            old["name"],
            old["scope"],
            project=old["project_id"],
            expires_at=old["expires_at"],
            created_by=old["created_by"],
        )
        ends = iso(datetime.now(UTC) + timedelta(seconds=max(0.0, grace_seconds)))
        with self.db.begin() as conn:
            if old["expires_at"] is None or old["expires_at"] > ends:
                self.db.execute(
                    sa.update(T).where(*self._pk(T, token_id)).values(expires_at=ends), conn
                )
            self._audit("token.rotate", token_id, {"successor": successor["id"]}, conn)
        self._forget_token(token_id)
        return successor

    def authenticate_token(self, secret: str, client: str | None = None) -> Principal | None:
        """The principal a token stands for, or None. This is how a request finds its workspace,
        so it is not scoped to one; a verified token is trusted for ``TOKEN_CACHE_TTL`` seconds."""
        if not tok.looks_like_a_token(secret):
            return None
        digest = tok.digest(secret)
        now = time.monotonic()
        with self._tokens.lock:
            cached = self._tokens.cache.get(digest)
        if cached and cached[0] > now and (cached[2] is None or cached[2] > now_iso()):
            return cached[1]
        row = self.db.for_workspace(None).one(
            sa.select(T, WS.c.deleted_at.label("workspace_deleted_at"))
            .select_from(T.join(WS, WS.c.id == T.c.workspace_id))
            .where(T.c.token_hash == digest)
        )
        if (
            row is None
            or row["workspace_deleted_at"] is not None
            or row["revoked_at"] is not None
            or (row["expires_at"] is not None and row["expires_at"] <= now_iso())
        ):
            with self._tokens.lock:
                self._tokens.cache.pop(digest, None)
            return None
        principal = Principal(
            kind="token",
            id=row["id"],
            workspace_id=row["workspace_id"],
            scope=row["scope"],
            project=row["project_id"],
            name=row["name"],
        )
        with self._tokens.lock:
            self._tokens.cache[digest] = (now + TOKEN_CACHE_TTL, principal, row["expires_at"])
            due = now - self._tokens.touched.get(row["id"], -TOKEN_TOUCH_EVERY) >= TOKEN_TOUCH_EVERY
            if due:
                self._tokens.touched[row["id"]] = now
        if due:
            self.db.for_workspace(None).execute(
                sa.update(T)
                .where(T.c.workspace_id == row["workspace_id"], T.c.id == row["id"])
                .values(last_used_at=now_iso(), last_used_from=tok.coarse_address(client))
            )
        return principal

    def _token_row(self, token_id: str) -> Any:
        row = self.db.one(sa.select(T).where(*self._pk(T, token_id)))
        if not row:
            raise SemulaError(404, "token_not_found", f"No token {token_id}.")
        return row

    def _forget_token(self, token_id: str) -> None:
        with self._tokens.lock:
            for digest, (_, principal, _) in list(self._tokens.cache.items()):
                if principal.id == token_id:
                    del self._tokens.cache[digest]

    @staticmethod
    def _token_view(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "scope": row["scope"],
            "project": row["project_id"],
            "prefix": row["prefix"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "last_used_at": row["last_used_at"],
            "last_used_from": row["last_used_from"],
        }

    # --- quotas (ADR-0011 section 6) -------------------------------------------------------
    def _ensure_usage(self, conn: Connection) -> None:
        values = {"workspace_id": self.workspace_id, "projects": 0, "entries": 0, "bytes": 0}
        if self.db.dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt: Any = pg_insert(U).values(**values).on_conflict_do_nothing()
        else:
            from sqlalchemy.dialects.sqlite import insert as lite_insert

            stmt = lite_insert(U).values(**values).on_conflict_do_nothing()
        self.db.execute(stmt, conn)

    def _reserve(
        self, conn: Connection, *, projects: int = 0, entries: int = 0, size: int = 0
    ) -> None:
        """Add to this workspace's counters inside the write's own transaction, refusing the
        write (and so rolling it back) if a limit would be crossed. One guarded UPDATE, so
        concurrent writers cannot both slip past a limit; it never runs on a replay hit."""
        limits = self.settings.quotas
        deltas = {"projects": projects, "entries": entries, "bytes": size}
        caps = {"projects": limits.projects, "entries": limits.entries, "bytes": limits.bytes}
        values: dict[str, Any] = {}
        guards: list[Any] = []
        for name, delta in deltas.items():
            if not delta:
                continue
            grown = U.c[name] + delta
            values[name] = sa.case((grown < 0, 0), else_=grown)
            if delta > 0 and caps[name] is not None:
                guards.append(grown <= caps[name])
        if not values:
            return
        self._ensure_usage(conn)
        done = self.db.execute(
            sa.update(U).where(U.c.workspace_id == self.workspace_id, *guards).values(**values),
            conn,
        )
        if done:
            return
        row = self.db.one(sa.select(U).where(U.c.workspace_id == self.workspace_id), conn)
        for name, delta in deltas.items():
            cap = caps[name]
            if delta > 0 and cap is not None and row and row[name] + delta > cap:
                raise self._over("projects" if name == "projects" else name, cap, int(row[name]))
        raise RuntimeError("a quota update matched no row")  # pragma: no cover

    @staticmethod
    def _over(name: str, limit: int, used: int, status: int = 403) -> SemulaError:
        what = {"bytes": "stored bytes"}.get(name, name.replace("_", " "))
        code = "quota_exceeded" if status == 403 else "too_many_parked"
        return SemulaError(
            status,
            code,
            f"This workspace has reached its limit of {limit} {what}.",
            extra={"quota": {"name": name, "limit": limit, "used": used}},
        )

    @staticmethod
    def _entry_size(canonical_request: str, responses: str) -> int:
        return len(canonical_request) + len(responses)

    def usage(self) -> dict[str, Any]:
        """What this workspace holds against what it may: counters kept as writes happen, plus
        the live number of parked calls and active tokens. Whole-workspace figures, so not for a
        token bound to one project."""
        if self.project_limit is not None:
            raise SemulaError(
                403,
                "forbidden",
                "A token bound to a project cannot see the workspace's usage.",
                err_type="authentication_error",
            )
        row = self.db.one(sa.select(U).where(U.c.workspace_id == self.workspace_id))
        parked = self.db.one(
            sa.select(sa.func.count().label("n")).where(
                Q.c.workspace_id == self.workspace_id, Q.c.status == "pending"
            )
        )
        now = now_iso()
        live = self.db.one(
            sa.select(sa.func.count().label("n")).where(
                T.c.workspace_id == self.workspace_id,
                T.c.revoked_at.is_(None),
                sa.or_(T.c.expires_at.is_(None), T.c.expires_at > now),
            )
        )
        q = self.settings.quotas
        return {
            "used": {
                "projects": int(row["projects"]) if row else 0,
                "entries": int(row["entries"]) if row else 0,
                "bytes": int(row["bytes"]) if row else 0,
                "parked": int(parked["n"]) if parked else 0,
                "tokens": int(live["n"]) if live else 0,
            },
            "limits": {
                "projects": q.projects,
                "entries": q.entries,
                "bytes": q.bytes,
                "parked_per_project": q.parked_per_project,
                "parked_per_workspace": q.parked_per_workspace,
                "tokens": q.tokens,
                "members": q.members,
            },
        }

    def recount_usage(self) -> dict[str, int]:
        """Rebuild the counters from what is stored (if they ever drift) and return them."""
        self._not_a_token()
        with self.db.begin() as conn:
            self._ensure_usage(conn)
            projects = self.db.one(
                sa.select(sa.func.count().label("n")).where(P.c.workspace_id == self.workspace_id),
                conn,
            )
            entries = self.db.one(
                sa.select(
                    sa.func.count().label("n"),
                    sa.func.coalesce(
                        sa.func.sum(
                            sa.func.length(E.c.canonical_request) + sa.func.length(E.c.responses)
                        ),
                        0,
                    ).label("size"),
                ).where(E.c.workspace_id == self.workspace_id),
                conn,
            )
            self.db.execute(
                sa.update(U)
                .where(U.c.workspace_id == self.workspace_id)
                .values(
                    projects=projects["n"] if projects else 0,
                    entries=entries["n"] if entries else 0,
                    bytes=int(entries["size"]) if entries else 0,
                ),
                conn,
            )
        used: dict[str, int] = self.usage()["used"]
        return used

    # --- audit log (ADR-0010 section 4) ---------------------------------------------------
    def _audit(
        self,
        action: str,
        target: str | None = None,
        detail: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> None:
        """Record who did ``action`` to ``target``, in ``conn``'s transaction when there is one.
        ``detail`` is for counts and key names only: never a prompt, an answer or a secret."""
        who = self.principal
        self.db.execute(
            sa.insert(AU).values(
                workspace_id=self.workspace_id,
                principal_kind=who.kind if who else "local",
                principal_id=who.id if who else "local",
                action=action,
                target=target,
                detail=json.dumps(detail, sort_keys=True) if detail else None,
                request_id=self.request_id,
                client=self.client,
            ),
            conn,
        )

    def list_audit(
        self, limit: int = 50, before_id: int | None = None, action: str | None = None
    ) -> list[dict[str, Any]]:
        """This workspace's audit log, newest first. For people and the operator, not tokens."""
        self._not_a_token()
        conditions = [AU.c.workspace_id == self.workspace_id]
        if before_id is not None:
            conditions.append(AU.c.id < before_id)
        if action:
            conditions.append(AU.c.action == action)
        rows = self.db.rows(
            sa.select(AU).where(*conditions).order_by(AU.c.id.desc()).limit(max(1, min(limit, 500)))
        )
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "principal_kind": r["principal_kind"],
                "principal_id": r["principal_id"],
                "action": r["action"],
                "target": r["target"],
                "detail": json.loads(r["detail"]) if r["detail"] else None,
                "request_id": r["request_id"],
                "client": r["client"],
            }
            for r in rows
        ]

    def prune_audit(self) -> int:
        """Delete this workspace's audit rows past the retention (the only way a row leaves)."""
        cutoff = iso(datetime.now(UTC) - timedelta(days=AUDIT_RETENTION_DAYS))
        return self.db.execute(
            sa.delete(AU).where(AU.c.workspace_id == self.workspace_id, AU.c.ts < cutoff)
        )

    def create_workspace(self, workspace_id: str, name: str | None = None) -> None:
        """Add a workspace (a tenant). Local use has only ``default``; this is for hosting."""
        if self.db.one(sa.select(WS.c.id).where(WS.c.id == workspace_id)):
            raise SemulaError(409, "workspace_exists", f"Workspace '{workspace_id}' exists.")
        with self.db.begin() as conn:  # a workspace starts with the project calls fall back to
            self.db.execute(sa.insert(WS).values(id=workspace_id, name=name or workspace_id), conn)
            self.db.for_workspace(workspace_id).execute(
                sa.insert(P).values(workspace_id=workspace_id, id=DEFAULT_PROJECT, name="Default"),
                conn,
            )
            fresh = self.for_scope(
                Scope(workspace_id, self.principal, self.request_id, self.client)
            )
            fresh._reserve(conn, projects=1)  # the default project
            fresh._audit("workspace.create", workspace_id, conn=conn)

    # --- deleting a workspace (ADR-0011 section 5) ------------------------------------------
    def workspace_status(self) -> dict[str, Any]:
        """Whether this workspace is deleted, and when it will be purged for good."""
        row = self.db.one(sa.select(WS.c.deleted_at).where(WS.c.id == self.workspace_id))
        deleted = row["deleted_at"] if row else None
        purge_after = (
            iso(
                datetime.fromisoformat(deleted.replace("Z", "+00:00"))
                + timedelta(days=WORKSPACE_GRACE_DAYS)
            )
            if deleted
            else None
        )
        return {"id": self.workspace_id, "deleted_at": deleted, "purge_after": purge_after}

    def delete_workspace(self, confirm: str) -> dict[str, Any]:
        """Delete this workspace: its tokens stop working at once and it is purged after
        ``WORKSPACE_GRACE_DAYS``, until when it can still be exported or restored. The workspace
        id must be typed back, and only a person or the operator may do it, never a token."""
        self._not_a_token()
        if self.workspace_id == DEFAULT_WORKSPACE:
            raise SemulaError(409, "default_workspace", "The default workspace cannot be deleted.")
        if confirm != self.workspace_id:
            raise SemulaError(
                422,
                "confirmation_mismatch",
                f"Type the workspace id, '{self.workspace_id}', to confirm.",
                param="confirm",
            )
        with self.db.begin() as conn:
            self.db.execute(
                sa.update(WS)
                .where(WS.c.id == self.workspace_id, WS.c.deleted_at.is_(None))
                .values(deleted_at=now_iso()),
                conn,
            )
            self._audit("workspace.delete", self.workspace_id, conn=conn)
        self._forget_workspace_tokens()
        return self.workspace_status()

    def restore_workspace(self) -> dict[str, Any]:
        """Bring a deleted workspace back during its grace period, tokens and all."""
        self._not_a_token()
        if self.workspace_status()["deleted_at"] is None:
            raise SemulaError(409, "workspace_not_deleted", "This workspace is not deleted.")
        with self.db.begin() as conn:
            self.db.execute(
                sa.update(WS).where(WS.c.id == self.workspace_id).values(deleted_at=None), conn
            )
            self._audit("workspace.restore", self.workspace_id, conn=conn)
        return self.workspace_status()

    def purge_workspaces(self) -> list[str]:
        """Hard-delete every workspace whose grace period is over, in dependency order, and say
        which. The operator's job (run it from a schedule); the audit log keeps the fact."""
        cutoff = iso(datetime.now(UTC) - timedelta(days=WORKSPACE_GRACE_DAYS))
        rows = self.db.rows(
            sa.select(WS.c.id).where(WS.c.deleted_at.is_not(None), WS.c.deleted_at < cutoff)
        )
        purged = []
        for row in rows:
            self.for_scope(Scope(row["id"], self.principal, self.request_id, self.client))._purge()
            purged.append(row["id"])
        return purged

    def _purge(self) -> None:
        ws = self.workspace_id
        with self.db.begin() as conn:
            self._audit("workspace.purge", ws, conn=conn)  # the one thing left behind, no content
            self.db.execute(sa.delete(L).where(L.c.workspace_id == ws), conn)
            self.lexical.delete_workspace(ws, conn)
            # projects, entries and everything under them, and tokens, go with the workspace row
            self.db.execute(sa.delete(U).where(U.c.workspace_id == ws), conn)
            self.db.execute(sa.delete(WS).where(WS.c.id == ws), conn)
        self._forget_workspace_tokens()

    def _forget_workspace_tokens(self) -> None:
        with self._tokens.lock:
            for digest, (_, principal, _) in list(self._tokens.cache.items()):
                if principal.workspace_id == self.workspace_id:
                    del self._tokens.cache[digest]

    def mode_of(self, project: str) -> str:
        """What a call to ``project`` does when nothing answers it: the project's own mode,
        else the server's default."""
        return self.project_settings(project).mode or self.default_mode

    def set_mode(self, mode: str, project: str | None = None) -> dict[str, Any]:
        """Switch between replay and record.

        For a ``project`` the choice is saved with it (``persisted``) and survives restarts.
        Without one it moves the server's default, which projects that have not chosen follow;
        that lives in memory only, so a restart returns it to ``SEMULA_MODE``.
        """
        if mode not in ("replay", "record"):
            raise SemulaError(
                422, "invalid_mode", "mode must be 'replay' or 'record'.", param="mode"
            )
        if project is None and (self.project_limit is not None or self.settings.hosted):
            raise SemulaError(
                403,
                "forbidden",
                "The default mode belongs to the operator; set a project's own mode.",
                err_type="authentication_error",
            )
        if project is not None:
            self.update_project(project, settings={"mode": mode})  # announces the change
            return {"mode": self.mode_of(project), "persisted": True, "project": project}
        self.default_mode = mode
        self._publish({"type": "mode", "project": None, "mode": mode})
        return {"mode": mode, "persisted": False, "project": None}

    # --- projects -----------------------------------------------------------------
    def create_project(self, slug: str, name: str | None = None) -> dict[str, Any]:
        if self.project_limit is not None:
            raise SemulaError(
                403, "forbidden", "A token bound to a project cannot create projects."
            )
        if not _SLUG.fullmatch(slug):
            raise SemulaError(
                422,
                "invalid_project_id",
                "A project id is 1-63 characters: lowercase letters, digits and hyphens, "
                "starting with a letter or digit.",
                param="id",
            )
        if self.db.one(sa.select(P.c.id).where(*self._pk(P, slug))):
            raise SemulaError(409, "project_exists", f"Project '{slug}' already exists.")
        with self.db.begin() as conn:
            self.db.execute(
                sa.insert(P).values(workspace_id=self.workspace_id, id=slug, name=name or slug),
                conn,
            )
            self._reserve(conn, projects=1)
            self._audit("project.create", slug, conn=conn)
        self._publish({"type": "projects"})
        return self.get_project(slug)

    def list_projects(self) -> list[dict[str, Any]]:
        entries = (
            sa.select(sa.func.count())
            .select_from(E)
            .where(E.c.workspace_id == P.c.workspace_id, E.c.project_id == P.c.id)
            .scalar_subquery()
        )
        parked = (
            sa.select(sa.func.count())
            .select_from(Q)
            .where(
                Q.c.workspace_id == P.c.workspace_id,
                Q.c.project_id == P.c.id,
                Q.c.status == "pending",
            )
            .scalar_subquery()
        )
        rows = self.db.rows(
            sa.select(
                P.c.id,
                P.c.name,
                P.c.created_at,
                entries.label("entries"),
                parked.label("pending"),
            )
            .where(
                P.c.workspace_id == self.workspace_id,
                *([P.c.id == self.project_limit] if self.project_limit else []),
            )
            .order_by(P.c.id)
        )
        return [{**r, "mode": self.mode_of(r["id"])} for r in map(dict, rows)]

    def get_project(self, slug: str) -> dict[str, Any]:
        for project in self.list_projects():
            if project["id"] == slug:
                project["settings"] = self.project_settings(slug).model_dump()
                return project
        raise SemulaError(404, "project_not_found", f"No project '{slug}'.", param="project")

    def require_project(self, slug: str) -> str:
        self.project_settings(slug)
        return slug

    def project_settings(self, slug: str) -> ProjectSettings:
        """A project's settings. Read on every call, so cached for ``SETTINGS_TTL`` seconds;
        a change made here is visible at once, one made by another replica within the TTL."""
        self._allowed(slug)
        cached = self._settings_cache.get((self.workspace_id, slug))
        if cached and time.monotonic() - cached[0] < SETTINGS_TTL:
            return cached[1]
        row = self.db.one(sa.select(P.c.settings).where(*self._pk(P, slug)))
        if not row:
            self._settings_cache.pop((self.workspace_id, slug), None)
            raise SemulaError(404, "project_not_found", f"No project '{slug}'.", param="project")
        settings = ProjectSettings.model_validate(json.loads(row["settings"]))
        self._settings_cache[(self.workspace_id, slug)] = (time.monotonic(), settings)
        return settings

    def update_project(
        self, slug: str, name: str | None = None, settings: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Rename and/or patch settings. A ``null`` value resets that setting to its default."""
        before = self.project_settings(slug)
        self._settings_cache.pop((self.workspace_id, slug), None)
        if name:
            with self.db.begin() as conn:
                self.db.execute(sa.update(P).where(*self._pk(P, slug)).values(name=name), conn)
                self._audit("project.rename", slug, {"name": name}, conn)
        if settings:
            merged = before.model_dump(exclude_defaults=True)
            for key, value in settings.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            try:
                after = ProjectSettings.model_validate(merged)
            except ValidationError as exc:
                first = exc.errors()[0]
                where = ".".join(["settings", *(str(p) for p in first["loc"])])
                raise SemulaError(
                    422,
                    "invalid_settings",
                    str(first["msg"]).removeprefix("Value error, "),
                    param=where,
                ) from exc
            longest = self.settings.quotas.record_timeout_max
            if longest is not None and after.record_timeout and after.record_timeout > longest:
                raise SemulaError(
                    422,
                    "invalid_settings",
                    f"record_timeout can be at most {longest:g} seconds on this server.",
                    param="settings.record_timeout",
                )
            with self.db.begin() as conn:
                self.db.execute(
                    sa.update(P)
                    .where(*self._pk(P, slug))
                    .values(settings=after.model_dump_json(exclude_defaults=True)),
                    conn,
                )
                if any(getattr(before, f) != getattr(after, f) for f in FINGERPRINT_FIELDS):
                    self._rekey(slug, after, conn)
                changed = sorted(
                    k
                    for k in settings
                    if k != "mode" and getattr(before, k, None) != getattr(after, k, None)
                )
                if changed:
                    self._audit("project.settings", slug, {"changed": changed}, conn)
                if "mode" in settings and settings["mode"] != before.mode:
                    self._audit("project.mode", slug, {"mode": after.mode}, conn)
        self._settings_cache.pop((self.workspace_id, slug), None)
        if settings and "mode" in settings and settings["mode"] != before.mode:
            self._publish({"type": "mode", "project": slug, "mode": self.mode_of(slug)})
        return self.get_project(slug)

    def _rekey(self, slug: str, settings: ProjectSettings, conn: Connection) -> None:
        """Recompute fingerprints after a matching setting changed, from stored requests."""
        config = settings.fingerprint_config()
        for table in (E, Q, A, C):
            rows = self.db.rows(
                sa.select(table.c.id, table.c.canonical_request).where(
                    table.c.workspace_id == self.workspace_id, table.c.project_id == slug
                ),
                conn,
            )
            for row in rows:
                req = redact_request(CanonicalRequest.from_json(row["canonical_request"]), config)
                values = {
                    "fingerprint": normalised_fingerprint(req, config),
                    # a stricter redaction list also cleans what was stored before it
                    "canonical_request": req.to_json(),
                }
                if "exact_fingerprint" in table.c:  # a candidate has no exact fingerprint
                    values["exact_fingerprint"] = exact_fingerprint(req, config)
                self.db.execute(
                    sa.update(table).where(*self._pk(table, row["id"])).values(**values), conn
                )
                if table is E:  # scrubbers and tail_n shape the indexed words too
                    self._index(row["id"], slug, req, config, conn)
        self._reredact_log(slug, config, conn)

    def _reredact_log(self, slug: str, config: FingerprintConfig, conn: Connection) -> None:
        """Apply the redaction list to the call-log previews already stored. A secret the 120
        character cut had already sliced in half cannot be recognised again: it is the one thing
        a later pattern cannot clean."""
        rows = self.db.rows(
            sa.select(L.c.id, L.c.preview).where(
                L.c.workspace_id == self.workspace_id,
                L.c.project_id == slug,
                L.c.preview.is_not(None),
            ),
            conn,
        )
        for row in rows:
            clean = canonical.redact_text(row["preview"], config.redactions)
            if clean != row["preview"]:
                self.db.execute(sa.update(L).where(L.c.id == row["id"]).values(preview=clean), conn)

    def delete_project(self, slug: str) -> None:
        self.require_project(slug)
        self._settings_cache.pop((self.workspace_id, slug), None)
        if slug == DEFAULT_PROJECT:
            raise SemulaError(409, "default_project", "The default project cannot be deleted.")
        parked = self.db.rows(
            sa.select(Q.c.id).where(Q.c.workspace_id == self.workspace_id, Q.c.project_id == slug)
        )
        with self.db.begin() as conn:
            gone = self.db.one(
                sa.select(
                    sa.func.count().label("n"),
                    sa.func.coalesce(
                        sa.func.sum(
                            sa.func.length(E.c.canonical_request) + sa.func.length(E.c.responses)
                        ),
                        0,
                    ).label("size"),
                ).where(E.c.workspace_id == self.workspace_id, E.c.project_id == slug),
                conn,
            )
            if gone:
                self._reserve(conn, projects=-1, entries=-int(gone["n"]), size=-int(gone["size"]))
            # entries, pending rows, candidates and aliases go with the project (ON DELETE CASCADE);
            # call_log has no FK
            self.db.execute(
                sa.delete(L).where(L.c.workspace_id == self.workspace_id, L.c.project_id == slug),
                conn,
            )
            self.db.execute(sa.delete(P).where(*self._pk(P, slug)), conn)
            self.lexical.delete_project(self.workspace_id, slug, conn)
            self._audit("project.delete", slug, conn=conn)
        for row in parked:
            self._release(row["id"])  # held calls find no answer and time out cleanly
        self._publish({"type": "projects"})

    # --- what live views hear besides calls -------------------------------------------
    def _queues_changed(self, project: str) -> None:
        """Tell live views that the pending or review queue of ``project`` moved. They count
        again; the event carries no numbers, so it cannot go stale."""
        self._publish({"type": "queues", "project": project})

    def _publish(self, event: dict[str, Any]) -> None:
        """Announce ``event`` to live views, tagged with the workspace it happened in."""
        self.events.publish({**event, "workspace": self.workspace_id})

    def owns(self, event: dict[str, Any]) -> bool:
        """Is ``event`` about this service's workspace? Live views and streams skip the rest."""
        return bool(event.get("workspace", DEFAULT_WORKSPACE) == self.workspace_id)

    def _entries_changed(self, project: str) -> None:
        """Tell live views that an entry of ``project`` was added, changed or removed."""
        self._publish({"type": "entries", "project": project})

    def queue_counts(self, project: str | None = None) -> dict[str, int]:
        """How many requests are parked and how many near-matches wait for review, for one
        project or all of them."""
        project = self._narrow(project)
        mine_q = [Q.c.workspace_id == self.workspace_id, Q.c.status == "pending"]
        mine_c = [C.c.workspace_id == self.workspace_id, C.c.status == "open"]
        if project:
            self.require_project(project)
            mine_q.append(Q.c.project_id == project)
            mine_c.append(C.c.project_id == project)
        pending = self.db.one(sa.select(sa.func.count().label("n")).select_from(Q).where(*mine_q))
        review = self.db.one(sa.select(sa.func.count().label("n")).select_from(C).where(*mine_c))
        return {
            "pending": int(pending["n"]) if pending else 0,
            "review": int(review["n"]) if review else 0,
        }

    # --- models -----------------------------------------------------------------
    def list_models(self, project: str = DEFAULT_PROJECT) -> list[str]:
        """Model ids for /v1/models: the project ``models`` setting, else the env default."""
        configured = self.project_settings(project).models
        return list(configured) if configured else list(self.settings.models)

    def record_timeout(self, project: str) -> float:
        return self.project_settings(project).record_timeout or self.settings.record_timeout

    # --- matching -------------------------------------------------------------
    def find(self, req: CanonicalRequest, project: str = DEFAULT_PROJECT) -> Match | None:
        return self.lookup(req, project).match

    def lookup(
        self, req: CanonicalRequest, project: str = DEFAULT_PROJECT, trace: Trace | None = None
    ) -> MatchResult:
        """Run the tiers: exact, normalised, approved alias, then lexical (ADR-0003). Pure: it
        serves nothing and writes nothing, so ``test_match`` can share it. Pass a ``trace`` to
        have each tier that ran say what it saw; the serving path passes none and pays nothing."""
        settings = self.project_settings(project)
        config = settings.fingerprint_config()
        match = self._find_exact(req, project, config, trace)
        if match:
            return MatchResult(match, [])
        found = self._find_lexical(req, project, settings, config, trace)
        if found.match or not self.vectors:
            return found
        return self._add_semantic(self.vectors, found, req, project, settings, config, trace)

    def _find_exact(
        self,
        req: CanonicalRequest,
        project: str,
        config: FingerprintConfig,
        trace: Trace | None = None,
    ) -> Match | None:
        fp = normalised_fingerprint(req, config)
        for tier, column, value in (
            ("exact", E.c.exact_fingerprint, exact_fingerprint(req, config)),
            ("normalised", E.c.fingerprint, fp),
        ):
            row = self.db.one(
                _entry_for_serving()
                .where(
                    E.c.workspace_id == self.workspace_id,
                    E.c.project_id == project,
                    column == value,
                )
                .order_by(E.c.created_at, E.c.id)
                .limit(1)
            )
            if row:
                if trace is not None:
                    trace[tier] = (
                        "match",
                        1.0,
                        "same prompt" if tier == "exact" else "same once cleaned",
                    )
                return _match(row, tier)
            if trace is not None:
                trace[tier] = ("no", None, "")
        approved = self.db.one(
            _entry_for_serving()
            .join(A, sa.and_(A.c.workspace_id == E.c.workspace_id, A.c.entry_id == E.c.id))
            .where(
                A.c.workspace_id == self.workspace_id,
                A.c.project_id == project,
                A.c.fingerprint == fp,
            )
            .order_by(A.c.created_at, A.c.id)
            .limit(1)
        )
        if trace is not None:
            trace["alias"] = ("match", 1.0, "approved") if approved else ("no", None, "")
        return _match(approved, "alias") if approved else None

    def _find_lexical(
        self,
        req: CanonicalRequest,
        project: str,
        settings: ProjectSettings,
        config: FingerprintConfig,
        trace: Trace | None = None,
    ) -> MatchResult:
        """Score the index's proposals by word overlap. Serve one only if it clears the
        project's high bar *and* has the same conversation shape; keep the rest as candidates."""
        words = lexical_tokens(req, config)
        hits = self.lexical.search(self.workspace_id, project, words, config, LEXICAL_POOL)
        if not hits:
            if trace is not None:
                trace["words"] = ("no", None, "no similar entry")
            return MatchResult(None, [])
        rejected = {
            r["entry_id"]
            for r in self.db.rows(
                sa.select(C.c.entry_id).where(
                    C.c.workspace_id == self.workspace_id,
                    C.c.project_id == project,
                    C.c.fingerprint == normalised_fingerprint(req, config),
                    C.c.status == "rejected",
                )
            )
        }
        scored = sorted(
            (
                (similarity(words, hit.tokens), hit.entry_id)
                for hit in hits
                if hit.entry_id not in rejected
            ),
            key=lambda t: -t[0],  # stable: ties keep the index's own order
        )
        scored = [(score, entry_id) for score, entry_id in scored if score > 0]
        if not scored:
            if trace is not None:
                trace["words"] = ("no", None, "no shared words")
            return MatchResult(None, [])
        my_shape = shape(req, config)
        for score, entry_id in scored:
            if score < settings.bm25_serve_threshold:
                break
            row = self.db.one(
                _entry_for_serving(E.c.canonical_request).where(*self._pk(E, entry_id))
            )
            if (
                row
                and shape(CanonicalRequest.from_json(row["canonical_request"]), config) == my_shape
            ):
                if trace is not None:
                    trace["words"] = ("match", round(score, 4), "same words")
                return MatchResult(_match(row, "bm25", score), [])
        best_score, best_id = scored[0]
        best = self.db.one(sa.select(E.c.canonical_request).where(*self._pk(E, best_id)))
        last = ""
        if best:
            messages = CanonicalRequest.from_json(best["canonical_request"]).messages
            last = messages[-1]["content"][:200] if messages else ""
        weak = self._same_turns(
            [(s, i) for s, i in scored if s >= settings.bm25_review_threshold], req, config
        )
        if trace is not None:
            if weak:
                trace["words"] = ("candidate", round(weak[0][0], 4), "in review")
            elif best_score >= settings.bm25_review_threshold:
                trace["words"] = ("no", round(best_score, 4), "other turns")
            else:
                trace["words"] = ("no", round(best_score, 4), "too different")
        return MatchResult(
            None,
            [Candidate(entry_id, round(score, 4)) for score, entry_id in weak[:MAX_CANDIDATES]],
            {"entry_id": best_id, "score": round(best_score, 3), "last_message": last},
        )

    def _add_semantic(
        self,
        vectors: VectorStore,
        found: MatchResult,
        req: CanonicalRequest,
        project: str,
        settings: ProjectSettings,
        config: FingerprintConfig,
        trace: Trace | None = None,
    ) -> MatchResult:
        """The embedding tier (ADR-0003). It runs only after every cheaper tier missed, since
        embedding the request is the slowest step. Its matches go to review; they are served only
        if the project opted in with ``embedding_serve_threshold``."""
        text = embedding_text(req, config)
        if not text:
            if trace is not None:
                trace["meaning"] = ("no", None, "no text")
            return found
        try:
            query = vectors.embedder.embed([text])[0]
            neighbours = vectors.search(self.workspace_id, project, query, LEXICAL_POOL)
        except Exception:  # an optional extra must never break a call that works without it
            log.warning("The embedding tier failed; continuing without it.", exc_info=True)
            if trace is not None:
                trace["meaning"] = ("no", None, "model failed")
            return found
        rejected = self._rejected(project, req, config)
        near = [
            (n.score, n.entry_id)
            for n in neighbours
            if n.entry_id not in rejected and n.score >= settings.embedding_review_threshold
        ]
        if not near:
            if trace is not None:
                best = max((n.score for n in neighbours), default=None)
                trace["meaning"] = (
                    "no",
                    None if best is None else round(best, 4),
                    "too different" if best is not None else "no similar entry",
                )
            return found
        serve_at = settings.embedding_serve_threshold
        if serve_at is not None:
            my_shape = shape(req, config)
            for score, entry_id in near:
                if score < serve_at:
                    break
                row = self.db.one(
                    _entry_for_serving(E.c.canonical_request).where(*self._pk(E, entry_id))
                )
                other = CanonicalRequest.from_json(row["canonical_request"]) if row else None
                if row and other and shape(other, config) == my_shape:
                    if trace is not None:
                        trace["meaning"] = ("match", round(score, 4), "same meaning")
                    return MatchResult(_match(row, "embedding", score), [])
        merged = {c.entry_id: c for c in found.candidates}  # the word match explains itself better
        same = self._same_turns(near, req, config)
        for score, entry_id in same:
            merged.setdefault(entry_id, Candidate(entry_id, round(score, 4), "embedding"))
        if trace is not None:
            trace["meaning"] = (
                ("candidate", round(same[0][0], 4), "in review")
                if same
                else ("no", round(near[0][0], 4), "other turns")
            )
        ranked = sorted(merged.values(), key=lambda c: -c.score)[:MAX_CANDIDATES]
        return MatchResult(None, ranked, found.nearest)

    def _rejected(self, project: str, req: CanonicalRequest, config: FingerprintConfig) -> set[str]:
        return {
            r["entry_id"]
            for r in self.db.rows(
                sa.select(C.c.entry_id).where(
                    C.c.workspace_id == self.workspace_id,
                    C.c.project_id == project,
                    C.c.fingerprint == normalised_fingerprint(req, config),
                    C.c.status == "rejected",
                )
            )
        }

    # --- embeddings -----------------------------------------------------------------
    def _texts_to_embed(self, ids: list[str]) -> list[tuple[str, str, str, str]]:
        rows = self.db.rows(
            sa.select(E.c.id, E.c.workspace_id, E.c.project_id, E.c.canonical_request).where(
                E.c.workspace_id == self.workspace_id, E.c.id.in_(ids)
            )
        )
        configs: dict[str, FingerprintConfig] = {}
        out: list[tuple[str, str, str, str]] = []
        for row in rows:
            project = row["project_id"]
            if project not in configs:
                configs[project] = self.project_settings(project).fingerprint_config()
            text = embedding_text(
                CanonicalRequest.from_json(row["canonical_request"]), configs[project]
            )
            if text:
                out.append((row["id"], row["workspace_id"], project, text))
        return out

    def sync_embeddings(self) -> None:
        """Queue every entry that has no vector yet (a fresh start, a new model, old data)."""
        if not self.vectors or not self.indexer:
            return
        have = self.vectors.indexed(self.workspace_id)
        for row in self.db.rows(sa.select(E.c.id).where(E.c.workspace_id == self.workspace_id)):
            if row["id"] not in have:
                self.indexer.schedule(row["id"])

    def embeddings_status(self) -> dict[str, Any]:
        if not self.vectors or not self.indexer:
            return {"enabled": False}
        if self.project_limit is not None:  # counts span every project: not for a bound token
            return {"enabled": True, "model": self.vectors.embedder.model_id}
        entries = self.db.one(
            sa.select(sa.func.count().label("n"))
            .select_from(E)
            .where(E.c.workspace_id == self.workspace_id)
        )
        return {
            "enabled": True,
            "model": self.vectors.embedder.model_id,
            "entries": entries["n"] if entries else 0,
            "indexed": self.vectors.count(self.workspace_id),
            "backlog": self.indexer.backlog,
        }

    def _same_turns(
        self, weak: list[tuple[float, str]], req: CanonicalRequest, config: FingerprintConfig
    ) -> list[tuple[float, str]]:
        """Keep review candidates whose conversation has the same speakers in the same order."""
        if not weak:
            return weak
        rows = self.db.rows(
            sa.select(E.c.id, E.c.canonical_request).where(
                E.c.workspace_id == self.workspace_id, E.c.id.in_([i for _, i in weak])
            )
        )
        mine = roles_of(req, config)
        same = {
            r["id"]
            for r in rows
            if roles_of(CanonicalRequest.from_json(r["canonical_request"]), config) == mine
        }
        return [(s, i) for s, i in weak if i in same]

    # --- entries ------------------------------------------------------------------
    def list_entries(
        self,
        project: str,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        pinned: bool | None = None,
        has_alias: bool | None = None,
        tool_calls: bool | None = None,
        never_hit: bool | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> dict[str, Any]:
        """A lean projection: no request or response bodies, cheap for agents to scan.

        Filters combine with AND; a flag left out does not filter, ``false`` selects the
        opposite. Without ``sort`` rows come in creation order (ids are ULIDs); a named sort
        defaults to most-first (``order`` ``desc``). Ties fall back to the id so pages stay
        stable."""
        self.require_project(project)
        if sort not in (None, *ENTRY_SORTS):
            raise SemulaError(
                422, "invalid_param", f"sort must be one of {', '.join(ENTRY_SORTS)}.", param="sort"
            )
        if order not in (None, "asc", "desc"):
            raise SemulaError(422, "invalid_param", "order must be 'asc' or 'desc'.", param="order")
        conditions = [E.c.workspace_id == self.workspace_id, E.c.project_id == project]
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(E.c.canonical_request.ilike(f"%{escaped}%", escape="\\"))
        if pinned is not None:
            conditions.append(E.c.pinned.is_(pinned))
        if never_hit is not None:
            conditions.append(E.c.hit_count == 0 if never_hit else E.c.hit_count > 0)
        if has_alias is not None:
            exists = sa.exists().where(A.c.workspace_id == E.c.workspace_id, A.c.entry_id == E.c.id)
            conditions.append(exists if has_alias else ~exists)
        if tool_calls is not None:
            calls = E.c.responses.like(TOOL_CALLS_LIKE)
            conditions.append(calls if tool_calls else ~calls)
        total = self.db.one(sa.select(sa.func.count().label("n")).select_from(E).where(*conditions))
        alias_count = (
            sa.select(sa.func.count())
            .where(A.c.workspace_id == E.c.workspace_id, A.c.entry_id == E.c.id)
            .correlate(E)
            .scalar_subquery()
            .label("alias_count")
        )
        column = E.c.id if sort is None else getattr(E.c, ENTRY_SORTS[sort])
        descending = (order or ("asc" if sort is None else "desc")) == "desc"
        ordering = [column.desc() if descending else column.asc()]
        if sort is not None:
            ordering.append(E.c.id.desc() if descending else E.c.id.asc())
        rows = self.db.rows(
            sa.select(E, alias_count)
            .where(*conditions)
            .order_by(*ordering)
            .limit(max(1, min(limit, 500)))
            .offset(max(0, offset))
        )
        return {
            "total": int(total["n"]) if total else 0,
            "entries": [_entry_view(r, full=False) for r in rows],
        }

    def upsert_entry(
        self,
        project: str,
        request_body: dict[str, Any],
        response: Any,
        *,
        version: int | None = None,
        actor: str = "agent",
        force: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Author an entry without waiting for a miss. Returns (entry, created).

        If an entry already matches this request, the caller must pass its current
        ``version``; the response then replaces the entry's responses.
        """
        self.require_project(project)
        if not isinstance(request_body.get("messages"), list):
            raise SemulaError(
                422, "invalid_request", "The request needs a 'messages' list.", param="request"
            )
        req = CanonicalRequest.from_json(canonical.from_openai(request_body).to_json())
        config = self.project_settings(project).fingerprint_config()
        fp = normalised_fingerprint(req, config)
        existing = self.db.one(
            sa.select(E.c.id)
            .where(
                E.c.workspace_id == self.workspace_id,
                E.c.project_id == project,
                E.c.fingerprint == fp,
            )
            .order_by(E.c.created_at, E.c.id)
            .limit(1)
        )
        if existing:
            if version is None:
                raise SemulaError(
                    409,
                    "entry_exists",
                    f"An entry already matches this request ({existing['id']}). "
                    "Pass its current 'version' to replace its response.",
                    param="version",
                    extra={"current": self.get_entry(existing["id"])},
                )
            entry = self.update_entry(
                existing["id"], version, {"response": response}, actor=actor, force=force
            )
            return entry, False
        stored = parse_answer(response)
        validate_tool_calls(stored, req.tools)
        entry_id = new_ulid()
        stored_request = redact_request(req, config).to_json()
        with self.db.begin() as conn:
            self._reserve(
                conn, entries=1, size=self._entry_size(stored_request, json.dumps([stored]))
            )
            self.db.execute(
                sa.insert(E).values(
                    id=entry_id,
                    workspace_id=self.workspace_id,
                    project_id=project,
                    fingerprint=fp,
                    exact_fingerprint=exact_fingerprint(req, config),
                    canonical_request=stored_request,
                    responses=json.dumps([stored]),
                ),
                conn,
            )
            self._index(entry_id, project, req, config, conn)
            superseded = self._supersede(conn, project, fp)
            self._revise(conn, entry_id, 1, actor, "created", [stored], "first", False)
        if superseded:
            self._queues_changed(project)
        self._entries_changed(project)
        return self.get_entry(entry_id), True

    def get_entry(self, entry_id: str) -> dict[str, Any]:
        return _entry_view(self._entry_row(entry_id), full=True)

    def _entry_row(self, entry_id: str) -> Any:
        row = self.db.one(sa.select(E).where(*self._pk(E, entry_id)))
        if not row:
            raise SemulaError(404, "entry_not_found", f"No entry {entry_id}.")
        self._own(row["project_id"], "entry_not_found", f"No entry {entry_id}.")
        return row

    def _guard_write(
        self, row: Any, version: int | None, actor: str, force: bool, action: str
    ) -> bool:
        """Optimistic concurrency and pin protection. Returns True when a pin was forced."""
        if not isinstance(version, int) or isinstance(version, bool):
            raise SemulaError(
                400,
                "version_required",
                f"Say which version you are {action}: pass the entry's current 'version'.",
                param="version",
            )
        view = _entry_view(row, full=True)
        if version != row["version"]:
            raise SemulaError(
                409,
                "version_conflict",
                f"Entry {row['id']} is at version {row['version']}, not {version}. "
                "Re-read it and apply your change to the current version.",
                param="version",
                extra={"current": view},
            )
        if row["pinned"] and actor != "human":
            if not force:
                raise SemulaError(
                    409,
                    "entry_pinned",
                    f"Entry {row['id']} is pinned by a human. Agents cannot change it unless "
                    "forced; ask a human, or pass force=true if you were told to.",
                    extra={"current": view},
                )
            return True
        return False

    def update_entry(
        self,
        entry_id: str,
        version: int | None,
        changes: dict[str, Any],
        *,
        actor: str = "agent",
        force: bool = False,
    ) -> dict[str, Any]:
        """Edit an entry. ``changes`` may hold response/responses/add_response/selection/pinned."""
        row = self._entry_row(entry_id)
        forced = self._guard_write(row, version, actor, force, "editing")
        tools = CanonicalRequest.from_json(row["canonical_request"]).tools
        responses = json.loads(row["responses"])

        def checked(payload: Any) -> dict[str, Any]:
            stored = parse_answer(payload)
            validate_tool_calls(stored, tools)
            return stored

        known = {"response", "responses", "add_response", "selection", "pinned"}
        unknown = set(changes) - known
        if unknown or not changes:
            raise SemulaError(
                400,
                "nothing_to_update" if not changes else "unknown_field",
                f"Send at least one of {sorted(known)}."
                if not changes
                else f"Unknown field(s): {sorted(unknown)}.",
            )
        if "response" in changes:
            responses = [checked(changes["response"])]
        if "responses" in changes:
            if not isinstance(changes["responses"], list) or not changes["responses"]:
                raise SemulaError(
                    422,
                    "invalid_answer",
                    "'responses' must be a non-empty list.",
                    param="responses",
                )
            responses = [checked(r) for r in changes["responses"]]
        if "add_response" in changes:
            responses.append(checked(changes["add_response"]))
        selection = changes.get("selection", row["selection"])
        if selection not in SELECTIONS:
            raise SemulaError(
                422,
                "invalid_selection",
                f"selection must be one of {list(SELECTIONS)}.",
                param="selection",
            )
        pinned = bool(row["pinned"])
        if "pinned" in changes:
            if not isinstance(changes["pinned"], bool):
                raise SemulaError(
                    422, "invalid_pinned", "pinned must be true or false.", param="pinned"
                )
            pinned = changes["pinned"]
        with self.db.begin() as conn:
            self._reserve(conn, size=len(json.dumps(responses)) - len(row["responses"]))
            changed = self.db.execute(
                sa.update(E)
                .where(*self._pk(E, entry_id), E.c.version == version)
                .values(
                    responses=json.dumps(responses),
                    selection=selection,
                    pinned=pinned,
                    version=E.c.version + 1,
                    updated_at=now_iso(),
                ),
                conn,
            )
            if changed != 1:  # lost a race between the check and the write
                raise SemulaError(409, "version_conflict", "The entry changed; re-read it.")
            summary = _change_summary(row, changes, responses, selection, pinned)
            self._revise(
                conn,
                entry_id,
                row["version"] + 1,  # the guard and the WHERE above made this the version we hold
                actor,
                summary,
                responses,
                selection,
                pinned,
                forced,
            )
            if forced:
                self._log_forced(row, "forced_write", conn)
                self._audit("entry.forced_write", entry_id, {"project": row["project_id"]}, conn)
        self._entries_changed(row["project_id"])
        return self.get_entry(entry_id)

    # --- history ------------------------------------------------------------------
    def _revise(
        self,
        conn: Connection,
        entry_id: str,
        version: int,
        actor: str,
        summary: str,
        responses: list[Any],
        selection: str,
        pinned: bool,
        forced: bool = False,
    ) -> None:
        """Snapshot an entry as a write leaves it, in that write's transaction, and drop the
        oldest snapshots beyond ``MAX_REVISIONS``."""
        self.db.execute(
            sa.insert(R).values(
                entry_id=entry_id,
                workspace_id=self.workspace_id,
                version=version,
                actor=actor if actor == "human" else "agent",
                summary=summary[:120],
                forced=forced,
                snapshot=json.dumps(
                    {"responses": responses, "selection": selection, "pinned": pinned}
                ),
            ),
            conn,
        )
        edge = self.db.one(
            sa.select(R.c.version)
            .where(*self._rk(entry_id))
            .order_by(R.c.version.desc())
            .offset(MAX_REVISIONS - 1)
            .limit(1),
            conn,
        )
        if edge:
            self.db.execute(
                sa.delete(R).where(*self._rk(entry_id), R.c.version < edge["version"]), conn
            )

    def entry_history(self, entry_id: str, version: int | None = None) -> dict[str, Any]:
        """The entry's revisions, newest first and without their snapshots; or, with ``version``,
        that one revision with its snapshot (the entry's responses, selection and pin then)."""
        entry = self._entry_row(entry_id)
        base = {"entry_id": entry_id, "version": entry["version"]}
        if version is not None:
            row = self.db.one(sa.select(R).where(*self._rk(entry_id), R.c.version == version))
            if not row:
                raise SemulaError(
                    404,
                    "revision_not_found",
                    f"Entry {entry_id} has no version {version} in its history "
                    f"(the latest {MAX_REVISIONS} are kept).",
                    param="version",
                )
            return {
                **base,
                "revision": {**_revision_view(row), "snapshot": json.loads(row["snapshot"])},
            }
        rows = self.db.rows(sa.select(R).where(*self._rk(entry_id)).order_by(R.c.version.desc()))
        return {**base, "revisions": [_revision_view(r) for r in rows]}

    def delete_entry(
        self, entry_id: str, version: int | None, *, actor: str = "agent", force: bool = False
    ) -> None:
        row = self._entry_row(entry_id)
        forced = self._guard_write(row, version, actor, force, "deleting")
        with self.db.begin() as conn:
            deleted = self.db.execute(
                sa.delete(E).where(*self._pk(E, entry_id), E.c.version == version), conn
            )
            if deleted != 1:
                raise SemulaError(409, "version_conflict", "The entry changed; re-read it.")
            self._reserve(
                conn,
                entries=-1,
                size=-self._entry_size(row["canonical_request"], row["responses"]),
            )
            self.lexical.delete(entry_id, self.workspace_id, conn)  # candidates, aliases cascade
            if forced:
                self._log_forced(row, "forced_delete", conn)
                self._audit("entry.forced_delete", entry_id, {"project": row["project_id"]}, conn)
            else:
                self._audit("entry.delete", entry_id, {"project": row["project_id"]}, conn)
        self._queues_changed(row["project_id"])  # candidates of the entry went with it
        self._entries_changed(row["project_id"])

    def _log_forced(self, row: Any, outcome: str, conn: Connection) -> None:
        self.db.execute(
            sa.insert(L).values(
                workspace_id=row["workspace_id"],
                project_id=row["project_id"],
                mode=self.mode_of(row["project_id"]),
                outcome=outcome,
                entry_id=row["id"],
            ),
            conn,
        )

    def test_match(self, req: CanonicalRequest, project: str) -> dict[str, Any]:
        """What would happen to this request right now, without serving, logging or counting."""
        self.require_project(project)
        trace: Trace = {}
        found = self.lookup(req, project, trace)
        match = found.match
        config = self.project_settings(project).fingerprint_config()
        out: dict[str, Any] = {
            "project": project,
            "mode": self.mode_of(project),
            "matched": match is not None,
            "tier": match.tier if match else None,
            "entry_id": match.entry_id if match else None,
            "score": match.score if match else None,
            "fingerprint": normalised_fingerprint(req, config),
            "candidates": [
                {"entry_id": c.entry_id, "score": c.score, "tier": c.tier} for c in found.candidates
            ],
            "nearest": None if match else found.nearest,
            "tiers": self._tier_verdicts(trace),
        }
        if match:
            out["would"] = "serve"
            content = match.stored["message"].get("content") or ""
            out["response_preview"] = content[:200]
        else:
            out["would"] = "park" if self.mode_of(project) == "record" else "no_match_error"
        return out

    def _tier_verdicts(self, trace: Trace) -> list[dict[str, Any]]:
        """One entry per tier, in cascade order. A tier the trace holds ran; one it lacks was
        either never reached (an earlier tier answered) or, for meaning, switched off."""
        out: list[dict[str, Any]] = []
        for tier in TIERS:
            if tier in trace:
                verdict, score, reason = trace[tier]
            elif tier == "meaning" and not self.vectors:
                verdict, score, reason = "off", None, ""
            else:
                verdict, score, reason = "skipped", None, "already answered"
            row: dict[str, Any] = {"tier": tier, "verdict": verdict}
            if score is not None:
                row["score"] = score
            if reason:
                row["reason"] = reason
            out.append(row)
        return out

    # --- lexical index ------------------------------------------------------------
    def _index(
        self,
        entry_id: str,
        project: str,
        req: CanonicalRequest,
        config: FingerprintConfig,
        conn: Connection,
    ) -> None:
        self.lexical.upsert(entry_id, self.workspace_id, project, lexical_tokens(req, config), conn)
        if self.indexer:
            self.indexer.schedule(entry_id)  # vectors are made off the write path

    def sync_index(self) -> None:
        """Make the lexical index match the entries of every workspace: add the missing, drop the
        stale. Cheap when nothing is out of step. Run at startup, which also fills a freshly
        migrated index."""
        for row in self.db.rows(sa.select(WS.c.id)):
            self.for_scope(Scope(row["id"]))._sync_workspace_index()

    def _sync_workspace_index(self) -> None:
        indexed = self.lexical.indexed_ids(self.workspace_id)
        if indexed is None:
            return
        rows = self.db.rows(
            sa.select(E.c.id, E.c.project_id, E.c.canonical_request).where(
                E.c.workspace_id == self.workspace_id
            )
        )
        known = {r["id"] for r in rows}
        configs: dict[str, FingerprintConfig] = {}
        with self.db.begin() as conn:
            for row in rows:
                if row["id"] in indexed:
                    continue
                project = row["project_id"]
                if project not in configs:
                    configs[project] = self.project_settings(project).fingerprint_config()
                req = CanonicalRequest.from_json(row["canonical_request"])
                self._index(row["id"], project, req, configs[project], conn)
            for stale in indexed - known:
                self.lexical.delete(stale, self.workspace_id, conn)

    # --- export and import ----------------------------------------------------
    def export_project(self, slug: str) -> dict[str, Any]:
        """Everything authored in a project as a ``semula/1`` bundle: entries (ULIDs kept) and
        approved aliases. Runtime state (hit counts, pending, the call log, the review queue)
        is left out so an unchanged project exports unchanged and diffs stay quiet."""
        project = self.get_project(slug)
        mine = (E.c.workspace_id == self.workspace_id, E.c.project_id == slug)
        entries = [
            {
                "id": r["id"],
                "request": json.loads(r["canonical_request"]),
                "responses": json.loads(r["responses"]),
                "selection": r["selection"],
                "pinned": bool(r["pinned"]),
                "version": r["version"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in self.db.rows(sa.select(E).where(*mine).order_by(E.c.id))
        ]
        aliases = [
            {
                "id": r["id"],
                "entry_id": r["entry_id"],
                "request": json.loads(r["canonical_request"]),
                "created_at": r["created_at"],
            }
            for r in self.db.rows(
                sa.select(A)
                .where(A.c.workspace_id == self.workspace_id, A.c.project_id == slug)
                .order_by(A.c.id)
            )
        ]
        return {
            "format": FORMAT,
            "project": {
                "id": slug,
                "name": project["name"],
                "settings": self.project_settings(slug).model_dump(
                    exclude_defaults=True, exclude={"mode"}
                ),
            },
            "entries": entries,
            "aliases": aliases,
        }

    def import_project(
        self,
        bundle: Any,
        project: str | None = None,
        *,
        force: bool = False,
        actor: str = "agent",
    ) -> dict[str, Any]:
        """Apply a bundle, all or nothing. Entries are matched by ULID: new ones are added,
        changed ones updated (a pinned entry is left alone unless ``force``), identical ones
        skipped, so importing twice changes nothing. Nothing is ever deleted. A project that
        does not exist is created with the bundle's settings; an existing one keeps its own."""
        manifest, entries, aliases = _parse_bundle(bundle)
        slug = self._allowed(project or str(manifest["project"]["id"]))
        if not _SLUG.fullmatch(slug):
            raise SemulaError(
                422, "invalid_project_id", f"'{slug}' is not a valid project id.", param="project"
            )
        report: dict[str, Any] = {
            "project": slug,
            "created_project": False,
            "entries": {"created": 0, "updated": 0, "unchanged": 0, "skipped_pinned": []},
            "aliases": {"created": 0, "updated": 0, "unchanged": 0},
        }
        with self.db.begin() as conn:
            if not self.db.one(sa.select(P.c.id).where(*self._pk(P, slug)), conn):
                settings = _bundle_settings(manifest["project"].get("settings"))
                self.db.execute(
                    sa.insert(P).values(
                        workspace_id=self.workspace_id,
                        id=slug,
                        name=manifest["project"].get("name") or slug,
                        settings=settings.model_dump_json(exclude_defaults=True),
                    ),
                    conn,
                )
                report["created_project"] = True
            config = self._config_in(slug, conn)
            for item in entries:
                self._import_entry(item, slug, config, force, report["entries"], conn, actor)
            known = {
                r["id"]
                for r in self.db.rows(
                    sa.select(E.c.id).where(
                        E.c.workspace_id == self.workspace_id, E.c.project_id == slug
                    ),
                    conn,
                )
            }
            for item in aliases:
                if item["entry_id"] not in known:
                    raise SemulaError(
                        422,
                        "invalid_bundle",
                        f"Alias {item['id']} points at entry {item['entry_id']}, which is in "
                        "neither the bundle nor the project.",
                        param="aliases",
                    )
                self._import_alias(item, slug, config, report["aliases"], conn)
            self._audit(
                "project.import",
                slug,
                {"entries": report["entries"], "aliases": report["aliases"]},
                conn,
            )
        if report["created_project"]:
            self._publish({"type": "projects"})
        self._queues_changed(slug)
        self._entries_changed(slug)
        return report

    def _config_in(self, slug: str, conn: Connection) -> FingerprintConfig:
        row = self.db.one(sa.select(P.c.settings).where(*self._pk(P, slug)), conn)
        raw = json.loads(row["settings"]) if row else {}
        return ProjectSettings.model_validate(raw).fingerprint_config()

    def _import_entry(
        self,
        item: dict[str, Any],
        slug: str,
        config: FingerprintConfig,
        force: bool,
        tally: dict[str, Any],
        conn: Connection,
        actor: str = "agent",
    ) -> None:
        req = CanonicalRequest.from_json(json.dumps(item["request"]))
        responses = []
        for number, raw in enumerate(item["responses"]):
            try:
                stored = parse_answer(raw)
                validate_tool_calls(stored, req.tools)
            except SemulaError as exc:
                raise SemulaError(
                    422,
                    "invalid_bundle",
                    f"Entry {item['id']}, response {number}: {exc.message}",
                    param="entries",
                ) from exc
            responses.append(stored)
        selection = item.get("selection", "first")
        if selection not in SELECTIONS:
            raise SemulaError(
                422,
                "invalid_bundle",
                f"Entry {item['id']}: selection must be one of {list(SELECTIONS)}.",
                param="entries",
            )
        stored_req = redact_request(req, config)
        values: dict[str, Any] = {
            "fingerprint": normalised_fingerprint(req, config),
            "exact_fingerprint": exact_fingerprint(req, config),
            "canonical_request": stored_req.to_json(),
            "responses": json.dumps(responses),
            "selection": selection,
            "pinned": bool(item.get("pinned", False)),
        }
        row = self.db.one(sa.select(E).where(*self._pk(E, item["id"])), conn)
        if row and (row["workspace_id"], row["project_id"]) != (self.workspace_id, slug):
            raise SemulaError(
                409,
                "entry_in_other_project",
                f"Entry {item['id']} already exists in project '{row['project_id']}'.",
                param="entries",
            )
        if row is None:
            extra = {k: item[k] for k in ("version", "created_at", "updated_at") if k in item}
            self._reserve(
                conn,
                entries=1,
                size=self._entry_size(values["canonical_request"], values["responses"]),
            )
            self.db.execute(
                sa.insert(E).values(
                    id=item["id"],
                    workspace_id=self.workspace_id,
                    project_id=slug,
                    **values,
                    **extra,
                ),
                conn,
            )
            tally["created"] += 1
            self._revise(
                conn,
                item["id"],
                extra.get("version", 1),
                actor,
                "imported",
                responses,
                selection,
                values["pinned"],
            )
        elif all(
            row[k] == v for k, v in values.items() if k not in ("fingerprint", "exact_fingerprint")
        ):
            tally["unchanged"] += 1
            return
        elif row["pinned"] and not force:
            tally["skipped_pinned"].append(item["id"])
            return
        else:
            self._reserve(
                conn,
                size=self._entry_size(values["canonical_request"], values["responses"])
                - self._entry_size(row["canonical_request"], row["responses"]),
            )
            self.db.execute(
                sa.update(E)
                .where(*self._pk(E, item["id"]))
                .values(**values, version=E.c.version + 1, updated_at=now_iso()),
                conn,
            )
            tally["updated"] += 1
            what = _change_summary(
                row,
                {"responses": responses} if row["responses"] != values["responses"] else {},
                responses,
                selection,
                values["pinned"],
            )
            self._revise(
                conn,
                item["id"],
                row["version"] + 1,
                actor,
                "imported" if what == "no change" else f"imported, {what}",
                responses,
                selection,
                values["pinned"],
                forced=bool(row["pinned"]),
            )
        self._index(item["id"], slug, req, config, conn)
        self._supersede(conn, slug, values["fingerprint"])

    def _import_alias(
        self,
        item: dict[str, Any],
        slug: str,
        config: FingerprintConfig,
        tally: dict[str, int],
        conn: Connection,
    ) -> None:
        req = CanonicalRequest.from_json(json.dumps(item["request"]))
        values = {
            "entry_id": item["entry_id"],
            "fingerprint": normalised_fingerprint(req, config),
            "exact_fingerprint": exact_fingerprint(req, config),
            "canonical_request": redact_request(req, config).to_json(),
        }
        row = self.db.one(sa.select(A).where(*self._pk(A, item["id"])), conn)
        if row and (row["workspace_id"], row["project_id"]) != (self.workspace_id, slug):
            raise SemulaError(
                409,
                "alias_in_other_project",
                f"Alias {item['id']} already exists in project '{row['project_id']}'.",
                param="aliases",
            )
        if row is None:
            extra = {"created_at": item["created_at"]} if "created_at" in item else {}
            self.db.execute(
                sa.insert(A).values(
                    id=item["id"],
                    workspace_id=self.workspace_id,
                    project_id=slug,
                    **values,
                    **extra,
                ),
                conn,
            )
            tally["created"] += 1
        elif all(row[k] == v for k, v in values.items()):
            tally["unchanged"] += 1
        else:
            self.db.execute(sa.update(A).where(*self._pk(A, item["id"])).values(**values), conn)
            tally["updated"] += 1
        self._supersede(conn, slug, values["fingerprint"])

    # --- review queue ---------------------------------------------------------------
    def record_candidates(
        self, req: CanonicalRequest, project: str, candidates: list[Candidate]
    ) -> None:
        """Queue weak lexical matches for a human. The call itself is still a miss. A pairing
        already queued or decided is not queued again, so a rejection is permanent."""
        if not candidates:
            return
        config = self.project_settings(project).fingerprint_config()
        fp = normalised_fingerprint(req, config)
        known = {
            r["entry_id"]
            for r in self.db.rows(
                sa.select(C.c.entry_id).where(
                    C.c.workspace_id == self.workspace_id,
                    C.c.project_id == project,
                    C.c.fingerprint == fp,
                )
            )
        }
        queued = False
        for found in candidates:
            if found.entry_id in known:
                continue
            queued = True
            self.db.execute(
                sa.insert(C).values(
                    id=new_ulid(),
                    workspace_id=self.workspace_id,
                    project_id=project,
                    entry_id=found.entry_id,
                    fingerprint=fp,
                    canonical_request=redact_request(req, config).to_json(),
                    score=found.score,
                    tier=found.tier,
                    status="open",
                )
            )
        if queued:
            self._queues_changed(project)

    def list_candidates(
        self, project: str | None = None, status: str | None = "open"
    ) -> list[dict[str, Any]]:
        project = self._narrow(project)
        conditions = [C.c.workspace_id == self.workspace_id]
        if project:
            self.require_project(project)
            conditions.append(C.c.project_id == project)
        if status:
            conditions.append(C.c.status == status)
        rows = self.db.rows(
            sa.select(C, E.c.canonical_request.label("entry_request"))
            .select_from(
                C.join(E, sa.and_(E.c.workspace_id == C.c.workspace_id, E.c.id == C.c.entry_id))
            )
            .where(*conditions)
            .order_by(C.c.score.desc(), C.c.id)
        )
        return [_candidate_view(r, full=False) for r in rows]

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        row = self._candidate_row(candidate_id)
        view = _candidate_view(row, full=True)
        view["entry"] = self.get_entry(row["entry_id"])
        return view

    def _candidate_row(self, candidate_id: str) -> Any:
        row = self.db.one(
            sa.select(C, E.c.canonical_request.label("entry_request"))
            .select_from(
                C.join(E, sa.and_(E.c.workspace_id == C.c.workspace_id, E.c.id == C.c.entry_id))
            )
            .where(*self._pk(C, candidate_id))
        )
        if not row:
            raise SemulaError(404, "candidate_not_found", f"No review candidate {candidate_id}.")
        self._own(row["project_id"], "candidate_not_found", f"No review candidate {candidate_id}.")
        return row

    def approve_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Say the request and the entry mean the same: the request now serves that entry."""
        row = self._candidate_row(candidate_id)
        config = self.project_settings(row["project_id"]).fingerprint_config()
        req = CanonicalRequest.from_json(row["canonical_request"])
        with self.db.begin() as conn:
            self._decide(candidate_id, "approved", conn)
            self.db.execute(
                sa.insert(A).values(
                    id=new_ulid(),
                    workspace_id=row["workspace_id"],
                    project_id=row["project_id"],
                    entry_id=row["entry_id"],
                    fingerprint=row["fingerprint"],
                    exact_fingerprint=exact_fingerprint(req, config),
                    canonical_request=row["canonical_request"],
                ),
                conn,
            )
            self._supersede(conn, row["project_id"], row["fingerprint"])  # others are moot now
            self._audit("candidate.approve", candidate_id, {"project": row["project_id"]}, conn)
        self._queues_changed(row["project_id"])
        return self.get_candidate(candidate_id)

    def reject_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Say they differ. The pairing is remembered and never queued or served again."""
        row = self._candidate_row(candidate_id)
        with self.db.begin() as conn:
            self._decide(candidate_id, "rejected", conn)
            self._audit("candidate.reject", candidate_id, {"project": row["project_id"]}, conn)
        self._queues_changed(row["project_id"])
        return self.get_candidate(candidate_id)

    def _decide(self, candidate_id: str, status: str, conn: Connection) -> None:
        changed = self.db.execute(
            sa.update(C)
            .where(*self._pk(C, candidate_id), C.c.status == "open")
            .values(status=status, decided_at=now_iso()),
            conn,
        )
        if changed != 1:
            current = self.db.one(sa.select(C.c.status).where(*self._pk(C, candidate_id)), conn)
            raise SemulaError(
                409,
                "candidate_decided",
                f"Candidate {candidate_id} is already {current['status'] if current else 'gone'}.",
            )

    def _supersede(self, conn: Connection, project: str, fingerprint: str) -> int:
        """Close open candidates for a request that now has an answer of its own."""
        return self.db.execute(
            sa.update(C)
            .where(
                C.c.workspace_id == self.workspace_id,
                C.c.project_id == project,
                C.c.fingerprint == fingerprint,
                C.c.status == "open",
            )
            .values(status="superseded", decided_at=now_iso()),
            conn,
        )

    # --- pending queue ----------------------------------------------------------
    def _check_parked(self, project: str) -> None:
        """Refuse to park one more call past the limits (this runs on a miss, never a hit)."""
        q = self.settings.quotas
        if q.parked_per_project is None and q.parked_per_workspace is None:
            return
        counts = self.db.rows(
            sa.select(Q.c.project_id, sa.func.count().label("n"))
            .where(Q.c.workspace_id == self.workspace_id, Q.c.status == "pending")
            .group_by(Q.c.project_id)
        )
        here = sum(int(r["n"]) for r in counts if r["project_id"] == project)
        total = sum(int(r["n"]) for r in counts)
        if q.parked_per_project is not None and here >= q.parked_per_project:
            raise self._over("parked_per_project", q.parked_per_project, here, 429)
        if q.parked_per_workspace is not None and total >= q.parked_per_workspace:
            raise self._over("parked_per_workspace", q.parked_per_workspace, total, 429)

    def open_pending(self, req: CanonicalRequest, project: str = DEFAULT_PROJECT) -> str:
        """Park a miss. A second identical miss joins the first instead of duplicating."""
        config = self.project_settings(project).fingerprint_config()
        fp = normalised_fingerprint(req, config)
        open_request = sa.select(Q.c.id).where(
            Q.c.workspace_id == self.workspace_id,
            Q.c.project_id == project,
            Q.c.fingerprint == fp,
            Q.c.status == "pending",
        )
        row = self.db.one(open_request)
        if row:
            return str(row["id"])
        self._check_parked(project)
        pid = new_ulid()
        try:
            self.db.execute(
                sa.insert(Q).values(
                    id=pid,
                    workspace_id=self.workspace_id,
                    project_id=project,
                    fingerprint=fp,
                    exact_fingerprint=exact_fingerprint(req, config),
                    canonical_request=redact_request(req, config).to_json(),
                )
            )
        except IntegrityError:  # another miss parked the same request first: join it
            row = self.db.one(open_request)
            if row is None:
                raise
            return str(row["id"])
        self._queues_changed(project)
        return pid

    async def wait_for_answer(self, pending_id: str, timeout: float) -> dict[str, Any] | None:
        """Wait for an answer. Returns the stored response, or None on timeout.

        The database reads run in a worker thread so a slow query never stalls the event loop.
        """
        event = asyncio.Event()
        with self._waiters_lock:
            self._waiters.setdefault(pending_id, []).append((asyncio.get_running_loop(), event))
        try:
            # Answered before we registered? Then nobody will wake us, so look first.
            stored = await run_in_threadpool(self._answer_for, pending_id)
            if stored is None:
                try:
                    await asyncio.wait_for(event.wait(), timeout)
                except TimeoutError:
                    return None
                stored = await run_in_threadpool(self._answer_for, pending_id)
            return stored
        finally:
            with self._waiters_lock:
                waiting = self._waiters.get(pending_id, [])
                waiting[:] = [w for w in waiting if w[1] is not event]
                if not waiting:
                    self._waiters.pop(pending_id, None)

    def _answer_for(self, pending_id: str) -> dict[str, Any] | None:
        row = self.db.one(
            sa.select(Q.c.entry_id).where(
                Q.c.id == pending_id, Q.c.workspace_id == self.workspace_id
            )
        )
        if not row or not row["entry_id"]:
            return None
        entry = self.db.one(
            sa.select(E.c.responses).where(
                E.c.id == row["entry_id"], E.c.workspace_id == self.workspace_id
            )
        )
        return json.loads(entry["responses"])[0] if entry else None

    def _release(self, pending_id: str) -> None:
        """Wake every call held on this request. Safe to call from any thread."""
        with self._waiters_lock:
            waiting = list(self._waiters.get(pending_id, []))
        for loop, event in waiting:
            loop.call_soon_threadsafe(event.set)

    def list_pending(
        self, status: str | None = "pending", project: str | None = None
    ) -> list[dict[str, Any]]:
        project = self._narrow(project)
        conditions = [Q.c.workspace_id == self.workspace_id]
        if status:
            conditions.append(Q.c.status == status)
        if project:
            self.require_project(project)
            conditions.append(Q.c.project_id == project)
        rows = self.db.rows(sa.select(Q).where(*conditions).order_by(Q.c.id))
        return [_pending_view(r, full=False) for r in rows]

    async def wait_for_pending(
        self, project: str | None = None, timeout: float = 30.0
    ) -> list[dict[str, Any]]:
        """The parked requests, waiting up to ``timeout`` seconds for one if none is parked.

        Subscribes before the first look, so a call parked in between is not missed. Every
        database read runs in a worker thread and none is open while waiting.
        """
        project = self._narrow(project)
        deadline = time.monotonic() + timeout
        with self.events.subscribe() as sub:
            while True:
                found = await run_in_threadpool(self.list_pending, "pending", project)
                remaining = deadline - time.monotonic()
                if found or remaining <= 0:
                    return found
                try:
                    async with asyncio.timeout(remaining):
                        while True:  # sleep until the queue of this project moves
                            event = await sub.get()
                            if (
                                event["type"] == "queues"
                                and self.owns(event)
                                and project in (None, event["project"])
                            ):
                                break
                except TimeoutError:
                    return await run_in_threadpool(self.list_pending, "pending", project)

    def get_pending(self, pending_id: str) -> dict[str, Any]:
        row = self.db.one(
            sa.select(Q).where(Q.c.id == pending_id, Q.c.workspace_id == self.workspace_id)
        )
        if not row:
            raise SemulaError(404, "pending_not_found", f"No pending request {pending_id}.")
        self._own(row["project_id"], "pending_not_found", f"No pending request {pending_id}.")
        return _pending_view(row, full=True)

    def answer_pending(self, pending_id: str, payload: Any, *, actor: str = "agent") -> str:
        """Validate an answer, save it as an entry, and release any held calls."""
        row = self.db.one(
            sa.select(Q).where(Q.c.id == pending_id, Q.c.workspace_id == self.workspace_id)
        )
        if not row:
            raise SemulaError(404, "pending_not_found", f"No pending request {pending_id}.")
        self._own(row["project_id"], "pending_not_found", f"No pending request {pending_id}.")
        if row["status"] != "pending":
            raise SemulaError(409, "already_answered", f"{pending_id} was already answered.")
        stored = parse_answer(payload)
        validate_tool_calls(stored, CanonicalRequest.from_json(row["canonical_request"]).tools)
        entry_id = new_ulid()
        with self.db.begin() as conn:  # the entry and the pending row change together or not at all
            claimed = self.db.execute(
                sa.update(Q)
                .where(*self._pk(Q, pending_id), Q.c.status == "pending")
                .values(status="answered", entry_id=entry_id, answered_at=now_iso()),
                conn,
            )
            if claimed != 1:  # someone answered between our read and this write
                raise SemulaError(409, "already_answered", f"{pending_id} was already answered.")
            self._reserve(
                conn,
                entries=1,
                size=self._entry_size(row["canonical_request"], json.dumps([stored])),
            )
            self.db.execute(
                sa.insert(E).values(
                    id=entry_id,
                    workspace_id=row["workspace_id"],
                    project_id=row["project_id"],
                    fingerprint=row["fingerprint"],
                    exact_fingerprint=row["exact_fingerprint"],
                    canonical_request=row["canonical_request"],
                    responses=json.dumps([stored]),
                ),
                conn,
            )
            req = CanonicalRequest.from_json(row["canonical_request"])
            config = self.project_settings(row["project_id"]).fingerprint_config()
            self._index(entry_id, row["project_id"], req, config, conn)
            self._supersede(conn, row["project_id"], row["fingerprint"])
            self._revise(conn, entry_id, 1, actor, "recorded", [stored], "first", False)
        self._release(pending_id)
        self._queues_changed(row["project_id"])
        self._entries_changed(row["project_id"])
        return entry_id

    # --- call log -----------------------------------------------------------------
    def log_call(
        self,
        outcome: str,
        started: float,
        *,
        tier: str | None = None,
        score: float | None = None,
        entry_id: str | None = None,
        pending_id: str | None = None,
        project: str = DEFAULT_PROJECT,
        count_hit: bool = False,
        req: CanonicalRequest | None = None,
    ) -> None:
        """Log one call, tell live subscribers, and now and then enforce retention. With
        ``count_hit`` the entry's hit count moves in the same transaction (one commit, not two).
        ``req`` is the request already in hand: its redacted preview goes on the row, with no
        extra query (project settings are cached)."""
        preview = (
            preview_text(req, self.project_settings(project).fingerprint_config()) if req else None
        )
        values = {
            "workspace_id": self.workspace_id,
            "project_id": project,
            "ts": now_iso(),
            "mode": self.mode_of(project),
            "outcome": outcome,
            "tier": tier,
            "score": score,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "entry_id": entry_id,
            "pending_id": pending_id,
            "preview": preview or None,
        }
        with self.db.begin() as conn:
            if count_hit and entry_id:
                self.db.execute(
                    sa.update(E).where(*self._pk(E, entry_id)).values(hit_count=E.c.hit_count + 1),
                    conn,
                )
            key = conn.execute(sa.insert(L).values(**values)).inserted_primary_key
        self._publish({"type": "call", "id": key[0] if key else None, **_log_view(values)})
        self._logged += 1
        if self._logged % PRUNE_EVERY == 0:
            self.prune_log()

    def list_calls(
        self,
        project: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Recent calls, newest first. ``before_id`` pages backwards."""
        project = self._narrow(project)
        conditions = [L.c.workspace_id == self.workspace_id]
        if project:
            self.require_project(project)
            conditions.append(L.c.project_id == project)
        if outcome:
            conditions.append(L.c.outcome == outcome)
        if before_id is not None:
            conditions.append(L.c.id < before_id)
        rows = self.db.rows(
            sa.select(L).where(*conditions).order_by(L.c.id.desc()).limit(max(1, min(limit, 500)))
        )
        return [{"id": r["id"], **_log_view(r)} for r in rows]

    def log_stats(
        self, project: str | None = None, window: str = "30m", *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Per-minute call counts for the last ``window`` (``30m``, ``2h``, ``1d``; one minute to
        24 hours), the hit rate, the median replay latency and how many calls are parked now.

        The buckets are the last N whole minutes, the current one included, so a window always
        has N of them (empty minutes are zero). Counting is a ``GROUP BY`` on the ISO timestamp's
        minute prefix and the median is taken in Python, so it behaves the same on SQLite and
        Postgres. ``hit_rate`` is hits over every call. ``median_latency_ms`` covers hits and
        misses only: a recorded or timed-out call waited for an agent, which is not replay speed.
        ``parked`` is the open pending requests now, whatever the window."""
        project = self._narrow(project)
        match = _WINDOW.fullmatch(window)
        minutes = int(match[1]) * {"m": 1, "h": 60, "d": 1440}[match[2]] if match else 0
        if not 1 <= minutes <= MAX_WINDOW_MINUTES:
            raise SemulaError(
                422,
                "invalid_param",
                "window must be a number of minutes, hours or days from 1m to 24h, like 30m.",
                param="window",
            )
        conditions = [L.c.workspace_id == self.workspace_id]
        pending = [Q.c.workspace_id == self.workspace_id, Q.c.status == "pending"]
        if project:
            self.require_project(project)
            conditions.append(L.c.project_id == project)
            pending.append(Q.c.project_id == project)
        now = (now or datetime.now(UTC)).astimezone(UTC)
        this_minute = now.replace(second=0, microsecond=0)
        slots = [this_minute - timedelta(minutes=minutes - 1 - i) for i in range(minutes)]
        window_start, window_end = iso(slots[0]), iso(this_minute + timedelta(minutes=1))
        conditions += [L.c.ts >= window_start, L.c.ts < window_end]
        minute = sa.func.substr(L.c.ts, 1, 16)  # 2026-10-21T10:30
        counted = self.db.rows(
            sa.select(minute.label("minute"), L.c.outcome, sa.func.count().label("n"))
            .where(*conditions)
            .group_by(minute, L.c.outcome)
        )
        buckets = {
            iso(slot)[:16]: {"ts": iso(slot)} | dict.fromkeys(STATS_OUTCOMES.values(), 0)
            for slot in slots
        }
        for row in counted:
            key = STATS_OUTCOMES.get(row["outcome"])
            if key and row["minute"] in buckets:
                buckets[row["minute"]][key] += row["n"]
        totals = {
            key: sum(int(b[key]) for b in buckets.values()) for key in STATS_OUTCOMES.values()
        }
        calls = sum(totals.values())
        latencies = [
            float(r["latency_ms"])
            for r in self.db.rows(
                sa.select(L.c.latency_ms).where(
                    *conditions, L.c.outcome.in_(("hit", "miss")), L.c.latency_ms.is_not(None)
                )
            )
        ]
        parked = self.db.one(sa.select(sa.func.count().label("n")).select_from(Q).where(*pending))
        return {
            "project": project,
            "window": window,
            "since": window_start,
            "until": iso(now),
            "buckets": list(buckets.values()),
            "totals": {"calls": calls, **totals},
            "hit_rate": round(totals["hits"] / calls, 4) if calls else None,
            "median_latency_ms": round(statistics.median(latencies), 3) if latencies else None,
            "parked": int(parked["n"]) if parked else 0,
        }

    def prune_log(self) -> int:
        """Drop calls older than the retention window, then the oldest beyond the row cap."""
        cutoff = iso(datetime.now(UTC) - timedelta(days=self.settings.log_retention_days))
        mine = L.c.workspace_id == self.workspace_id
        with self.db.begin() as conn:
            deleted = self.db.execute(sa.delete(L).where(mine, L.c.ts < cutoff), conn)
            edge = self.db.one(
                sa.select(L.c.id)
                .where(mine)
                .order_by(L.c.id.desc())
                .offset(max(1, self.settings.log_max_rows) - 1)
                .limit(1),
                conn,
            )
            if edge:
                deleted += self.db.execute(sa.delete(L).where(mine, L.c.id < edge["id"]), conn)
        return deleted


def _revision_view(row: Any) -> dict[str, Any]:
    return {
        "version": row["version"],
        "ts": row["ts"],
        "actor": row["actor"],
        "summary": row["summary"],
        "forced": bool(row["forced"]),
    }


def _change_summary(
    before: Any, changes: dict[str, Any], responses: list[Any], selection: str, pinned: bool
) -> str:
    """A few words on what a write changed, for the history list."""
    parts: list[str] = []
    if "response" in changes or "responses" in changes:
        parts.append("answer replaced" if "response" in changes else "answers replaced")
    if "add_response" in changes:
        parts.append(f"answer added ({len(responses)} in all)")
    if selection != before["selection"]:
        parts.append(f"selection {before['selection']} to {selection}")
    if pinned != bool(before["pinned"]):
        parts.append("pinned" if pinned else "unpinned")
    return ", ".join(parts) or "no change"


def _log_view(row: Any) -> dict[str, Any]:
    keys = ("ts", "project_id", "mode", "outcome", "tier", "score", "latency_ms", "entry_id")
    view = {k: row[k] for k in keys}
    view["project"] = view.pop("project_id")
    view["pending_id"] = row["pending_id"]
    view["preview"] = row["preview"]
    return view


def _bundle_settings(raw: Any) -> ProjectSettings:
    """Settings from a bundle. A bundle never sets the mode: whether a project records is a
    decision about this server, and importing a file must not switch it."""
    try:
        return ProjectSettings.model_validate(raw or {}).model_copy(update={"mode": None})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(["project", "settings", *(str(p) for p in first["loc"])])
        raise SemulaError(
            422, "invalid_bundle", str(first["msg"]).removeprefix("Value error, "), param=where
        ) from exc


def _parse_bundle(bundle: Any) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Check a bundle's shape before anything is written; raise ``invalid_bundle`` if wrong."""

    def bad(message: str, param: str | None = None) -> SemulaError:
        return SemulaError(422, "invalid_bundle", message, param=param)

    if not isinstance(bundle, dict):
        raise bad("The bundle must be a JSON object.")
    if bundle.get("format") != FORMAT:
        raise bad(
            f"Unsupported format {bundle.get('format')!r}; this semula reads '{FORMAT}'.", "format"
        )
    project = bundle.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("id"), str):
        raise bad("'project' needs an 'id'.", "project")
    lists: list[list[dict[str, Any]]] = []
    for kind, required in (
        ("entries", ("id", "request", "responses")),
        ("aliases", ("id", "entry_id", "request")),
    ):
        items = bundle.get(kind, [])
        if not isinstance(items, list):
            raise bad(f"'{kind}' must be a list.", kind)
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or any(k not in item for k in required):
                raise bad(f"Every item in '{kind}' needs {list(required)}.", kind)
            request = item["request"]
            if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
                raise bad(f"{kind[:-1].capitalize()} {item['id']} has no request messages.", kind)
            if kind == "entries" and (
                not isinstance(item["responses"], list) or not item["responses"]
            ):
                raise bad(f"Entry {item['id']} needs a non-empty 'responses' list.", kind)
            if item["id"] in seen:
                raise bad(f"Duplicate id {item['id']} in '{kind}'.", kind)
            seen.add(item["id"])
        lists.append(items)
    return {"project": project}, lists[0], lists[1]


def _entry_for_serving(*extra: Any) -> sa.Select[Any]:
    return sa.select(E.c.id, E.c.responses, E.c.selection, E.c.hit_count, *extra)


def _match(row: Any, tier: str, score: float = 1.0) -> Match:
    responses = json.loads(row["responses"])
    chosen = responses[_pick(row["selection"], row["hit_count"], len(responses))]
    return Match(row["id"], tier, chosen, score)


def _pick(selection: str, served: int, count: int) -> int:
    if selection == "sequence":
        return min(served, count - 1)
    if selection == "cycle":
        return served % count
    return 0


def _entry_view(row: Any, *, full: bool) -> dict[str, Any]:
    req = CanonicalRequest.from_json(row["canonical_request"])
    responses = json.loads(row["responses"])
    view: dict[str, Any] = {
        "id": row["id"],
        "project": row["project_id"],
        "version": row["version"],
        "pinned": bool(row["pinned"]),
        "selection": row["selection"],
        "hit_count": row["hit_count"],
        "responses": len(responses),
        "last_message": (req.messages[-1]["content"][:200] if req.messages else ""),
        "fingerprint": row["fingerprint"],
        "updated_at": row["updated_at"],
        "has_tools": any((r.get("message") or {}).get("tool_calls") for r in responses),
    }
    if "alias_count" in row:
        view["alias_count"] = row["alias_count"]
    if full:
        view["request"] = {"messages": req.messages, "tools": req.tools}
        view["responses"] = responses
        view["created_at"] = row["created_at"]
    return view


def _candidate_view(row: Any, *, full: bool) -> dict[str, Any]:
    req = CanonicalRequest.from_json(row["canonical_request"])
    other = CanonicalRequest.from_json(row["entry_request"])
    view: dict[str, Any] = {
        "id": row["id"],
        "project": row["project_id"],
        "status": row["status"],
        "tier": row["tier"],
        "score": row["score"],
        "entry_id": row["entry_id"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
        "last_message": (req.messages[-1]["content"][:200] if req.messages else ""),
        "entry_last_message": (other.messages[-1]["content"][:200] if other.messages else ""),
    }
    if full:
        view["request"] = {"messages": req.messages, "tools": req.tools}
    return view


def _pending_view(row: Any, *, full: bool) -> dict[str, Any]:
    req = CanonicalRequest.from_json(row["canonical_request"])
    view: dict[str, Any] = {
        "id": row["id"],
        "project": row["project_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "entry_id": row["entry_id"],
        "last_message": (req.messages[-1]["content"][:200] if req.messages else ""),
    }
    if full:
        view["request"] = {"messages": req.messages, "tools": req.tools}
    return view
