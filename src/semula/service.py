"""The service layer: the only code that reads or writes the database (ADR-0005)."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from . import canonical
from .canonical import (
    CanonicalRequest,
    exact_fingerprint,
    normalised_fingerprint,
    scrub,
)
from .config import Settings
from .db import Database
from .errors import SemulaError
from .ids import new_ulid
from .project_settings import FINGERPRINT_FIELDS, ProjectSettings
from .responses import parse_answer, validate_tool_calls
from .schema import DEFAULT_PROJECT, DEFAULT_WORKSPACE, now_iso
from .schema import call_log as L
from .schema import entry as E
from .schema import pending as Q
from .schema import project as P

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


SELECTIONS = ("first", "sequence", "cycle")


@dataclass(frozen=True)
class Match:
    entry_id: str
    tier: str
    stored: dict[str, Any]


class Service:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        # Held calls waiting on an answer: pending id -> [(their event loop, their event)].
        # Answers arrive on worker threads, so waking a waiter must go through its loop.
        self._waiters: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}
        self._waiters_lock = threading.Lock()
        self.mode: str = settings.mode

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
        self.get_project(slug)
        return slug

    def project_settings(self, slug: str) -> ProjectSettings:
        row = self.db.one(sa.select(P.c.settings).where(*_pk(P, slug)))
        if not row:
            raise SemulaError(404, "project_not_found", f"No project '{slug}'.", param="project")
        return ProjectSettings.model_validate(json.loads(row["settings"]))

    def update_project(
        self, slug: str, name: str | None = None, settings: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Rename and/or patch settings. A ``null`` value resets that setting to its default."""
        before = self.project_settings(slug)
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
                where = ".".join(str(p) for p in first["loc"])
                raise SemulaError(
                    422, "invalid_settings", str(first["msg"]), param=f"settings.{where}"
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
        return self.get_project(slug)

    def _rekey(self, slug: str, settings: ProjectSettings, conn: Connection) -> None:
        """Recompute fingerprints after a matching setting changed, from stored requests."""
        config = settings.fingerprint_config()
        for table in (E, Q):
            rows = self.db.rows(
                sa.select(table.c.id, table.c.canonical_request).where(
                    table.c.workspace_id == DEFAULT_WORKSPACE, table.c.project_id == slug
                ),
                conn,
            )
            for row in rows:
                req = CanonicalRequest.from_json(row["canonical_request"])
                self.db.execute(
                    sa.update(table)
                    .where(table.c.id == row["id"])
                    .values(
                        fingerprint=normalised_fingerprint(req, config),
                        exact_fingerprint=exact_fingerprint(req, config),
                    ),
                    conn,
                )

    def delete_project(self, slug: str) -> None:
        self.require_project(slug)
        if slug == DEFAULT_PROJECT:
            raise SemulaError(409, "default_project", "The default project cannot be deleted.")
        parked = self.db.rows(
            sa.select(Q.c.id).where(Q.c.workspace_id == DEFAULT_WORKSPACE, Q.c.project_id == slug)
        )
        with self.db.begin() as conn:
            # entry and pending rows go with the project (ON DELETE CASCADE); call_log has no FK
            self.db.execute(
                sa.delete(L).where(L.c.workspace_id == DEFAULT_WORKSPACE, L.c.project_id == slug),
                conn,
            )
            self.db.execute(sa.delete(P).where(*_pk(P, slug)), conn)
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
        config = self.project_settings(project).fingerprint_config()
        for tier, column, fp in (
            ("exact", E.c.exact_fingerprint, exact_fingerprint(req, config)),
            ("normalised", E.c.fingerprint, normalised_fingerprint(req, config)),
        ):
            row = self.db.one(
                sa.select(E.c.id, E.c.responses, E.c.selection, E.c.hit_count)
                .where(
                    E.c.workspace_id == DEFAULT_WORKSPACE,
                    E.c.project_id == project,
                    column == fp,
                )
                .order_by(E.c.created_at, E.c.id)
                .limit(1)
            )
            if row:
                responses = json.loads(row["responses"])
                chosen = responses[_pick(row["selection"], row["hit_count"], len(responses))]
                return Match(row["id"], tier, chosen)
        return None

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
        self.db.execute(
            sa.insert(E).values(
                id=entry_id,
                workspace_id=DEFAULT_WORKSPACE,
                project_id=project,
                fingerprint=fp,
                exact_fingerprint=exact_fingerprint(req, config),
                canonical_request=req.to_json(),
                responses=json.dumps([stored]),
            )
        )
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

    def nearest(self, req: CanonicalRequest, project: str) -> dict[str, Any] | None:
        """The most similar recorded entry, by word overlap. For humans and agents to debug a
        miss; it never serves an answer."""
        wanted = _tokens(req)
        if not wanted:
            return None
        best: tuple[float, Any] | None = None
        rows = self.db.rows(
            sa.select(E.c.id, E.c.canonical_request)
            .where(E.c.workspace_id == DEFAULT_WORKSPACE, E.c.project_id == project)
            .order_by(E.c.id)
            .limit(5000)
        )
        for row in rows:
            other = _tokens(CanonicalRequest.from_json(row["canonical_request"]))
            if not other:
                continue
            score = len(wanted & other) / len(wanted | other)
            if score > 0 and (best is None or score > best[0]):
                best = (score, row)
        if best is None:
            return None
        score, row = best
        last = CanonicalRequest.from_json(row["canonical_request"]).messages[-1]["content"]
        return {"entry_id": row["id"], "score": round(score, 3), "last_message": last[:200]}

    def test_match(self, req: CanonicalRequest, project: str) -> dict[str, Any]:
        """What would happen to this request right now, without serving, logging or counting."""
        self.require_project(project)
        match = self.find(req, project)
        config = self.project_settings(project).fingerprint_config()
        out: dict[str, Any] = {
            "project": project,
            "mode": self.mode,
            "matched": match is not None,
            "tier": match.tier if match else None,
            "entry_id": match.entry_id if match else None,
            "score": 1.0 if match else None,
            "fingerprint": normalised_fingerprint(req, config),
            "nearest": None if match else self.nearest(req, project),
        }
        if match:
            out["would"] = "serve"
            content = match.stored["message"].get("content") or ""
            out["response_preview"] = content[:200]
        else:
            out["would"] = "park" if self.mode == "record" else "no_match_error"
        return out

    def record_hit(self, entry_id: str) -> None:
        self.db.execute(sa.update(E).where(E.c.id == entry_id).values(hit_count=E.c.hit_count + 1))

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
                    canonical_request=req.to_json(),
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
        self._release(pending_id)
        return entry_id

    # --- call log -----------------------------------------------------------------
    def log_call(
        self,
        outcome: str,
        started: float,
        *,
        tier: str | None = None,
        entry_id: str | None = None,
        pending_id: str | None = None,
        project: str = DEFAULT_PROJECT,
    ) -> None:
        self.db.execute(
            sa.insert(L).values(
                workspace_id=DEFAULT_WORKSPACE,
                project_id=project,
                mode=self.mode,
                outcome=outcome,
                tier=tier,
                latency_ms=(time.perf_counter() - started) * 1000,
                entry_id=entry_id,
                pending_id=pending_id,
            )
        )


def _pk(table: sa.Table, slug: str) -> tuple[Any, Any]:
    return (table.c.workspace_id == DEFAULT_WORKSPACE, table.c.id == slug)


def _tokens(req: CanonicalRequest) -> set[str]:
    text = " ".join(m["content"] for m in req.messages if isinstance(m.get("content"), str))
    return set(re.findall(r"\w+", scrub(text).lower()))


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
