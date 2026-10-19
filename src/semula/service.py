"""The service layer: the only code that reads or writes the database (ADR-0005)."""

from __future__ import annotations

import asyncio
import json
import re
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
from .canonical import (
    CanonicalRequest,
    FingerprintConfig,
    exact_fingerprint,
    lexical_tokens,
    normalised_fingerprint,
    redact_request,
    roles_of,
    shape,
)
from .config import Settings
from .db import Database
from .errors import SemulaError
from .events import EventBus
from .exchange import FORMAT
from .ids import new_ulid
from .lexical import make_index, similarity
from .project_settings import FINGERPRINT_FIELDS, ProjectSettings
from .responses import parse_answer, validate_tool_calls
from .schema import DEFAULT_PROJECT, DEFAULT_WORKSPACE, iso, now_iso
from .schema import alias as A
from .schema import call_log as L
from .schema import candidate as C
from .schema import entry as E
from .schema import pending as Q
from .schema import project as P

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


SELECTIONS = ("first", "sequence", "cycle")

LEXICAL_POOL = 10  # entries the lexical index proposes before they are scored
MAX_CANDIDATES = 3  # weak matches queued for review per missed request
PRUNE_EVERY = 200  # call-log rows between retention sweeps
SETTINGS_TTL = 1.0  # seconds a project's settings are reused between reads


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


class Service:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        # Held calls waiting on an answer: pending id -> [(their event loop, their event)].
        # Answers arrive on worker threads, so waking a waiter must go through its loop.
        self._waiters: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}
        self._waiters_lock = threading.Lock()
        self.mode: str = settings.mode
        self.lexical = make_index(db)
        self.events = EventBus()
        self._settings_cache: dict[str, tuple[float, ProjectSettings]] = {}
        self._logged = 0

    # --- mode -----------------------------------------------------------------------
    def set_mode(self, mode: str) -> str:
        """Switch between replay and record at runtime. Not persisted: a restart returns to
        ``SEMULA_MODE``."""
        if mode not in ("replay", "record"):
            raise SemulaError(
                422, "invalid_mode", "mode must be 'replay' or 'record'.", param="mode"
            )
        self.mode = mode
        return mode

    # --- projects -----------------------------------------------------------------
    def create_project(self, slug: str, name: str | None = None) -> dict[str, Any]:
        if not _SLUG.fullmatch(slug):
            raise SemulaError(
                422,
                "invalid_project_id",
                "A project id is 1-63 characters: lowercase letters, digits and hyphens, "
                "starting with a letter or digit.",
                param="id",
            )
        if self.db.one(sa.select(P.c.id).where(*_pk(P, slug))):
            raise SemulaError(409, "project_exists", f"Project '{slug}' already exists.")
        self.db.execute(
            sa.insert(P).values(workspace_id=DEFAULT_WORKSPACE, id=slug, name=name or slug)
        )
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
            .where(P.c.workspace_id == DEFAULT_WORKSPACE)
            .order_by(P.c.id)
        )
        return [dict(r) for r in rows]

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
        cached = self._settings_cache.get(slug)
        if cached and time.monotonic() - cached[0] < SETTINGS_TTL:
            return cached[1]
        row = self.db.one(sa.select(P.c.settings).where(*_pk(P, slug)))
        if not row:
            self._settings_cache.pop(slug, None)
            raise SemulaError(404, "project_not_found", f"No project '{slug}'.", param="project")
        settings = ProjectSettings.model_validate(json.loads(row["settings"]))
        self._settings_cache[slug] = (time.monotonic(), settings)
        return settings

    def update_project(
        self, slug: str, name: str | None = None, settings: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Rename and/or patch settings. A ``null`` value resets that setting to its default."""
        before = self.project_settings(slug)
        self._settings_cache.pop(slug, None)
        if name:
            self.db.execute(sa.update(P).where(*_pk(P, slug)).values(name=name))
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
            with self.db.begin() as conn:
                self.db.execute(
                    sa.update(P)
                    .where(*_pk(P, slug))
                    .values(settings=after.model_dump_json(exclude_defaults=True)),
                    conn,
                )
                if any(getattr(before, f) != getattr(after, f) for f in FINGERPRINT_FIELDS):
                    self._rekey(slug, after, conn)
        self._settings_cache.pop(slug, None)
        return self.get_project(slug)

    def _rekey(self, slug: str, settings: ProjectSettings, conn: Connection) -> None:
        """Recompute fingerprints after a matching setting changed, from stored requests."""
        config = settings.fingerprint_config()
        for table in (E, Q, A, C):
            rows = self.db.rows(
                sa.select(table.c.id, table.c.canonical_request).where(
                    table.c.workspace_id == DEFAULT_WORKSPACE, table.c.project_id == slug
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
                    sa.update(table).where(table.c.id == row["id"]).values(**values), conn
                )
                if table is E:  # scrubbers and tail_n shape the indexed words too
                    self._index(row["id"], slug, req, config, conn)

    def delete_project(self, slug: str) -> None:
        self.require_project(slug)
        self._settings_cache.pop(slug, None)
        if slug == DEFAULT_PROJECT:
            raise SemulaError(409, "default_project", "The default project cannot be deleted.")
        parked = self.db.rows(
            sa.select(Q.c.id).where(Q.c.workspace_id == DEFAULT_WORKSPACE, Q.c.project_id == slug)
        )
        with self.db.begin() as conn:
            # entries, pending rows, candidates and aliases go with the project (ON DELETE CASCADE);
            # call_log has no FK
            self.db.execute(
                sa.delete(L).where(L.c.workspace_id == DEFAULT_WORKSPACE, L.c.project_id == slug),
                conn,
            )
            self.db.execute(sa.delete(P).where(*_pk(P, slug)), conn)
            self.lexical.delete_project(DEFAULT_WORKSPACE, slug, conn)
        for row in parked:
            self._release(row["id"])  # held calls find no answer and time out cleanly

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

    def lookup(self, req: CanonicalRequest, project: str = DEFAULT_PROJECT) -> MatchResult:
        """Run the tiers: exact, normalised, approved alias, then lexical (ADR-0003). Pure: it
        serves nothing and writes nothing, so ``test_match`` can share it."""
        settings = self.project_settings(project)
        config = settings.fingerprint_config()
        match = self._find_exact(req, project, config)
        if match:
            return MatchResult(match, [])
        return self._find_lexical(req, project, settings, config)

    def _find_exact(
        self, req: CanonicalRequest, project: str, config: FingerprintConfig
    ) -> Match | None:
        fp = normalised_fingerprint(req, config)
        for tier, column, value in (
            ("exact", E.c.exact_fingerprint, exact_fingerprint(req, config)),
            ("normalised", E.c.fingerprint, fp),
        ):
            row = self.db.one(
                _entry_for_serving()
                .where(
                    E.c.workspace_id == DEFAULT_WORKSPACE,
                    E.c.project_id == project,
                    column == value,
                )
                .order_by(E.c.created_at, E.c.id)
                .limit(1)
            )
            if row:
                return _match(row, tier)
        approved = self.db.one(
            _entry_for_serving()
            .join(A, A.c.entry_id == E.c.id)
            .where(
                A.c.workspace_id == DEFAULT_WORKSPACE,
                A.c.project_id == project,
                A.c.fingerprint == fp,
            )
            .order_by(A.c.created_at, A.c.id)
            .limit(1)
        )
        return _match(approved, "alias") if approved else None

    def _find_lexical(
        self,
        req: CanonicalRequest,
        project: str,
        settings: ProjectSettings,
        config: FingerprintConfig,
    ) -> MatchResult:
        """Score the index's proposals by word overlap. Serve one only if it clears the
        project's high bar *and* has the same conversation shape; keep the rest as candidates."""
        words = lexical_tokens(req, config)
        hits = self.lexical.search(DEFAULT_WORKSPACE, project, words, config, LEXICAL_POOL)
        if not hits:
            return MatchResult(None, [])
        rejected = {
            r["entry_id"]
            for r in self.db.rows(
                sa.select(C.c.entry_id).where(
                    C.c.workspace_id == DEFAULT_WORKSPACE,
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
            return MatchResult(None, [])
        my_shape = shape(req, config)
        for score, entry_id in scored:
            if score < settings.bm25_serve_threshold:
                break
            row = self.db.one(_entry_for_serving(E.c.canonical_request).where(E.c.id == entry_id))
            if (
                row
                and shape(CanonicalRequest.from_json(row["canonical_request"]), config) == my_shape
            ):
                return MatchResult(_match(row, "bm25", score), [])
        best_score, best_id = scored[0]
        best = self.db.one(sa.select(E.c.canonical_request).where(E.c.id == best_id))
        last = ""
        if best:
            messages = CanonicalRequest.from_json(best["canonical_request"]).messages
            last = messages[-1]["content"][:200] if messages else ""
        weak = self._same_turns(
            [(s, i) for s, i in scored if s >= settings.bm25_review_threshold], req, config
        )
        return MatchResult(
            None,
            [Candidate(entry_id, round(score, 4)) for score, entry_id in weak[:MAX_CANDIDATES]],
            {"entry_id": best_id, "score": round(best_score, 3), "last_message": last},
        )

    def _same_turns(
        self, weak: list[tuple[float, str]], req: CanonicalRequest, config: FingerprintConfig
    ) -> list[tuple[float, str]]:
        """Keep review candidates whose conversation has the same speakers in the same order."""
        if not weak:
            return weak
        rows = self.db.rows(
            sa.select(E.c.id, E.c.canonical_request).where(E.c.id.in_([i for _, i in weak]))
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
        self, project: str, q: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """A lean projection: no request or response bodies, cheap for agents to scan."""
        self.require_project(project)
        conditions = [E.c.workspace_id == DEFAULT_WORKSPACE, E.c.project_id == project]
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(E.c.canonical_request.ilike(f"%{escaped}%", escape="\\"))
        total = self.db.one(sa.select(sa.func.count().label("n")).select_from(E).where(*conditions))
        rows = self.db.rows(
            sa.select(E)
            .where(*conditions)
            .order_by(E.c.id)
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
                E.c.workspace_id == DEFAULT_WORKSPACE,
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
        with self.db.begin() as conn:
            self.db.execute(
                sa.insert(E).values(
                    id=entry_id,
                    workspace_id=DEFAULT_WORKSPACE,
                    project_id=project,
                    fingerprint=fp,
                    exact_fingerprint=exact_fingerprint(req, config),
                    canonical_request=redact_request(req, config).to_json(),
                    responses=json.dumps([stored]),
                ),
                conn,
            )
            self._index(entry_id, project, req, config, conn)
            self._supersede(conn, project, fp)
        return self.get_entry(entry_id), True

    def get_entry(self, entry_id: str) -> dict[str, Any]:
        return _entry_view(self._entry_row(entry_id), full=True)

    def _entry_row(self, entry_id: str) -> Any:
        row = self.db.one(
            sa.select(E).where(E.c.id == entry_id, E.c.workspace_id == DEFAULT_WORKSPACE)
        )
        if not row:
            raise SemulaError(404, "entry_not_found", f"No entry {entry_id}.")
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
            changed = self.db.execute(
                sa.update(E)
                .where(E.c.id == entry_id, E.c.version == version)
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
            if forced:
                self._log_forced(row, "forced_write", conn)
        return self.get_entry(entry_id)

    def delete_entry(
        self, entry_id: str, version: int | None, *, actor: str = "agent", force: bool = False
    ) -> None:
        row = self._entry_row(entry_id)
        forced = self._guard_write(row, version, actor, force, "deleting")
        with self.db.begin() as conn:
            deleted = self.db.execute(
                sa.delete(E).where(E.c.id == entry_id, E.c.version == version), conn
            )
            if deleted != 1:
                raise SemulaError(409, "version_conflict", "The entry changed; re-read it.")
            self.lexical.delete(entry_id, conn)  # candidates and aliases cascade
            if forced:
                self._log_forced(row, "forced_delete", conn)

    def _log_forced(self, row: Any, outcome: str, conn: Connection) -> None:
        self.db.execute(
            sa.insert(L).values(
                workspace_id=row["workspace_id"],
                project_id=row["project_id"],
                mode=self.mode,
                outcome=outcome,
                entry_id=row["id"],
            ),
            conn,
        )

    def test_match(self, req: CanonicalRequest, project: str) -> dict[str, Any]:
        """What would happen to this request right now, without serving, logging or counting."""
        self.require_project(project)
        found = self.lookup(req, project)
        match = found.match
        config = self.project_settings(project).fingerprint_config()
        out: dict[str, Any] = {
            "project": project,
            "mode": self.mode,
            "matched": match is not None,
            "tier": match.tier if match else None,
            "entry_id": match.entry_id if match else None,
            "score": match.score if match else None,
            "fingerprint": normalised_fingerprint(req, config),
            "candidates": [
                {"entry_id": c.entry_id, "score": c.score, "tier": c.tier} for c in found.candidates
            ],
            "nearest": None if match else found.nearest,
        }
        if match:
            out["would"] = "serve"
            content = match.stored["message"].get("content") or ""
            out["response_preview"] = content[:200]
        else:
            out["would"] = "park" if self.mode == "record" else "no_match_error"
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
        self.lexical.upsert(entry_id, DEFAULT_WORKSPACE, project, lexical_tokens(req, config), conn)

    def sync_index(self) -> None:
        """Make the lexical index match the entries: add the missing, drop the stale. Cheap
        when nothing is out of step. Run at startup, which also fills a freshly migrated index."""
        indexed = self.lexical.indexed_ids()
        if indexed is None:
            return
        rows = self.db.rows(
            sa.select(E.c.id, E.c.project_id, E.c.canonical_request).where(
                E.c.workspace_id == DEFAULT_WORKSPACE
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
                self.lexical.delete(stale, conn)

    # --- export and import ----------------------------------------------------
    def export_project(self, slug: str) -> dict[str, Any]:
        """Everything authored in a project as a ``semula/1`` bundle: entries (ULIDs kept) and
        approved aliases. Runtime state (hit counts, pending, the call log, the review queue)
        is left out so an unchanged project exports unchanged and diffs stay quiet."""
        project = self.get_project(slug)
        mine = (E.c.workspace_id == DEFAULT_WORKSPACE, E.c.project_id == slug)
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
                .where(A.c.workspace_id == DEFAULT_WORKSPACE, A.c.project_id == slug)
                .order_by(A.c.id)
            )
        ]
        return {
            "format": FORMAT,
            "project": {
                "id": slug,
                "name": project["name"],
                "settings": self.project_settings(slug).model_dump(exclude_defaults=True),
            },
            "entries": entries,
            "aliases": aliases,
        }

    def import_project(
        self, bundle: Any, project: str | None = None, *, force: bool = False
    ) -> dict[str, Any]:
        """Apply a bundle, all or nothing. Entries are matched by ULID: new ones are added,
        changed ones updated (a pinned entry is left alone unless ``force``), identical ones
        skipped, so importing twice changes nothing. Nothing is ever deleted. A project that
        does not exist is created with the bundle's settings; an existing one keeps its own."""
        manifest, entries, aliases = _parse_bundle(bundle)
        slug = project or str(manifest["project"]["id"])
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
            if not self.db.one(sa.select(P.c.id).where(*_pk(P, slug)), conn):
                settings = _bundle_settings(manifest["project"].get("settings"))
                self.db.execute(
                    sa.insert(P).values(
                        workspace_id=DEFAULT_WORKSPACE,
                        id=slug,
                        name=manifest["project"].get("name") or slug,
                        settings=settings.model_dump_json(exclude_defaults=True),
                    ),
                    conn,
                )
                report["created_project"] = True
            config = self._config_in(slug, conn)
            for item in entries:
                self._import_entry(item, slug, config, force, report["entries"], conn)
            known = {
                r["id"]
                for r in self.db.rows(
                    sa.select(E.c.id).where(
                        E.c.workspace_id == DEFAULT_WORKSPACE, E.c.project_id == slug
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
        return report

    def _config_in(self, slug: str, conn: Connection) -> FingerprintConfig:
        row = self.db.one(sa.select(P.c.settings).where(*_pk(P, slug)), conn)
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
        row = self.db.one(sa.select(E).where(E.c.id == item["id"]), conn)
        if row and (row["workspace_id"], row["project_id"]) != (DEFAULT_WORKSPACE, slug):
            raise SemulaError(
                409,
                "entry_in_other_project",
                f"Entry {item['id']} already exists in project '{row['project_id']}'.",
                param="entries",
            )
        if row is None:
            extra = {k: item[k] for k in ("version", "created_at", "updated_at") if k in item}
            self.db.execute(
                sa.insert(E).values(
                    id=item["id"],
                    workspace_id=DEFAULT_WORKSPACE,
                    project_id=slug,
                    **values,
                    **extra,
                ),
                conn,
            )
            tally["created"] += 1
        elif all(
            row[k] == v for k, v in values.items() if k not in ("fingerprint", "exact_fingerprint")
        ):
            tally["unchanged"] += 1
            return
        elif row["pinned"] and not force:
            tally["skipped_pinned"].append(item["id"])
            return
        else:
            self.db.execute(
                sa.update(E)
                .where(E.c.id == item["id"])
                .values(**values, version=E.c.version + 1, updated_at=now_iso()),
                conn,
            )
            tally["updated"] += 1
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
        row = self.db.one(sa.select(A).where(A.c.id == item["id"]), conn)
        if row and (row["workspace_id"], row["project_id"]) != (DEFAULT_WORKSPACE, slug):
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
                    workspace_id=DEFAULT_WORKSPACE,
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
            self.db.execute(sa.update(A).where(A.c.id == item["id"]).values(**values), conn)
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
                    C.c.workspace_id == DEFAULT_WORKSPACE,
                    C.c.project_id == project,
                    C.c.fingerprint == fp,
                )
            )
        }
        for found in candidates:
            if found.entry_id in known:
                continue
            self.db.execute(
                sa.insert(C).values(
                    id=new_ulid(),
                    workspace_id=DEFAULT_WORKSPACE,
                    project_id=project,
                    entry_id=found.entry_id,
                    fingerprint=fp,
                    canonical_request=redact_request(req, config).to_json(),
                    score=found.score,
                    tier=found.tier,
                    status="open",
                )
            )

    def list_candidates(
        self, project: str | None = None, status: str | None = "open"
    ) -> list[dict[str, Any]]:
        conditions = [C.c.workspace_id == DEFAULT_WORKSPACE]
        if project:
            self.require_project(project)
            conditions.append(C.c.project_id == project)
        if status:
            conditions.append(C.c.status == status)
        rows = self.db.rows(
            sa.select(C, E.c.canonical_request.label("entry_request"))
            .select_from(C.join(E, E.c.id == C.c.entry_id))
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
            .select_from(C.join(E, E.c.id == C.c.entry_id))
            .where(C.c.id == candidate_id, C.c.workspace_id == DEFAULT_WORKSPACE)
        )
        if not row:
            raise SemulaError(404, "candidate_not_found", f"No review candidate {candidate_id}.")
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
        return self.get_candidate(candidate_id)

    def reject_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Say they differ. The pairing is remembered and never queued or served again."""
        self._candidate_row(candidate_id)
        with self.db.begin() as conn:
            self._decide(candidate_id, "rejected", conn)
        return self.get_candidate(candidate_id)

    def _decide(self, candidate_id: str, status: str, conn: Connection) -> None:
        changed = self.db.execute(
            sa.update(C)
            .where(C.c.id == candidate_id, C.c.status == "open")
            .values(status=status, decided_at=now_iso()),
            conn,
        )
        if changed != 1:
            current = self.db.one(sa.select(C.c.status).where(C.c.id == candidate_id), conn)
            raise SemulaError(
                409,
                "candidate_decided",
                f"Candidate {candidate_id} is already {current['status'] if current else 'gone'}.",
            )

    def _supersede(self, conn: Connection, project: str, fingerprint: str) -> None:
        """Close open candidates for a request that now has an answer of its own."""
        self.db.execute(
            sa.update(C)
            .where(
                C.c.workspace_id == DEFAULT_WORKSPACE,
                C.c.project_id == project,
                C.c.fingerprint == fingerprint,
                C.c.status == "open",
            )
            .values(status="superseded", decided_at=now_iso()),
            conn,
        )

    # --- pending queue ----------------------------------------------------------
    def open_pending(self, req: CanonicalRequest, project: str = DEFAULT_PROJECT) -> str:
        """Park a miss. A second identical miss joins the first instead of duplicating."""
        config = self.project_settings(project).fingerprint_config()
        fp = normalised_fingerprint(req, config)
        open_request = sa.select(Q.c.id).where(
            Q.c.workspace_id == DEFAULT_WORKSPACE,
            Q.c.project_id == project,
            Q.c.fingerprint == fp,
            Q.c.status == "pending",
        )
        row = self.db.one(open_request)
        if row:
            return str(row["id"])
        pid = new_ulid()
        try:
            self.db.execute(
                sa.insert(Q).values(
                    id=pid,
                    workspace_id=DEFAULT_WORKSPACE,
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
        row = self.db.one(sa.select(Q.c.entry_id).where(Q.c.id == pending_id))
        if not row or not row["entry_id"]:
            return None
        entry = self.db.one(sa.select(E.c.responses).where(E.c.id == row["entry_id"]))
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
        conditions = [Q.c.workspace_id == DEFAULT_WORKSPACE]
        if status:
            conditions.append(Q.c.status == status)
        if project:
            self.require_project(project)
            conditions.append(Q.c.project_id == project)
        rows = self.db.rows(sa.select(Q).where(*conditions).order_by(Q.c.id))
        return [_pending_view(r, full=False) for r in rows]

    def get_pending(self, pending_id: str) -> dict[str, Any]:
        row = self.db.one(
            sa.select(Q).where(Q.c.id == pending_id, Q.c.workspace_id == DEFAULT_WORKSPACE)
        )
        if not row:
            raise SemulaError(404, "pending_not_found", f"No pending request {pending_id}.")
        return _pending_view(row, full=True)

    def answer_pending(self, pending_id: str, payload: Any) -> str:
        """Validate an answer, save it as an entry, and release any held calls."""
        row = self.db.one(
            sa.select(Q).where(Q.c.id == pending_id, Q.c.workspace_id == DEFAULT_WORKSPACE)
        )
        if not row:
            raise SemulaError(404, "pending_not_found", f"No pending request {pending_id}.")
        if row["status"] != "pending":
            raise SemulaError(409, "already_answered", f"{pending_id} was already answered.")
        stored = parse_answer(payload)
        validate_tool_calls(stored, CanonicalRequest.from_json(row["canonical_request"]).tools)
        entry_id = new_ulid()
        with self.db.begin() as conn:  # the entry and the pending row change together or not at all
            claimed = self.db.execute(
                sa.update(Q)
                .where(Q.c.id == pending_id, Q.c.status == "pending")
                .values(status="answered", entry_id=entry_id, answered_at=now_iso()),
                conn,
            )
            if claimed != 1:  # someone answered between our read and this write
                raise SemulaError(409, "already_answered", f"{pending_id} was already answered.")
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
        self._release(pending_id)
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
    ) -> None:
        """Log one call, tell live subscribers, and now and then enforce retention. With
        ``count_hit`` the entry's hit count moves in the same transaction (one commit, not two)."""
        values = {
            "workspace_id": DEFAULT_WORKSPACE,
            "project_id": project,
            "ts": now_iso(),
            "mode": self.mode,
            "outcome": outcome,
            "tier": tier,
            "score": score,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "entry_id": entry_id,
            "pending_id": pending_id,
        }
        with self.db.begin() as conn:
            if count_hit and entry_id:
                self.db.execute(
                    sa.update(E).where(E.c.id == entry_id).values(hit_count=E.c.hit_count + 1), conn
                )
            key = conn.execute(sa.insert(L).values(**values)).inserted_primary_key
        self.events.publish({"type": "call", "id": key[0] if key else None, **_log_view(values)})
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
        conditions = [L.c.workspace_id == DEFAULT_WORKSPACE]
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

    def prune_log(self) -> int:
        """Drop calls older than the retention window, then the oldest beyond the row cap."""
        cutoff = iso(datetime.now(UTC) - timedelta(days=self.settings.log_retention_days))
        mine = L.c.workspace_id == DEFAULT_WORKSPACE
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


def _log_view(row: Any) -> dict[str, Any]:
    keys = ("ts", "project_id", "mode", "outcome", "tier", "score", "latency_ms", "entry_id")
    view = {k: row[k] for k in keys}
    view["project"] = view.pop("project_id")
    view["pending_id"] = row["pending_id"]
    return view


def _bundle_settings(raw: Any) -> ProjectSettings:
    try:
        return ProjectSettings.model_validate(raw or {})
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


def _pk(table: sa.Table, slug: str) -> tuple[Any, Any]:
    return (table.c.workspace_id == DEFAULT_WORKSPACE, table.c.id == slug)


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
    }
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
