"""The service layer: the only code that reads or writes the database (ADR-0005)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .canonical import (
    CanonicalRequest,
    exact_fingerprint,
    normalised_fingerprint,
)
from .config import Settings
from .db import DEFAULT_PROJECT, DEFAULT_WORKSPACE, Database
from .errors import SemulaError
from .ids import new_ulid
from .project_settings import FINGERPRINT_FIELDS, ProjectSettings
from .responses import parse_answer, validate_tool_calls

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


@dataclass(frozen=True)
class Match:
    entry_id: str
    tier: str
    stored: dict[str, Any]


class Service:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self._waiters: dict[str, asyncio.Event] = {}

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
        if self.db.query_one(
            "SELECT 1 FROM project WHERE workspace_id=? AND id=?", (DEFAULT_WORKSPACE, slug)
        ):
            raise SemulaError(409, "project_exists", f"Project '{slug}' already exists.")
        self.db.execute(
            "INSERT INTO project (workspace_id, id, name) VALUES (?,?,?)",
            (DEFAULT_WORKSPACE, slug, name or slug),
        )
        return self.get_project(slug)

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT p.id, p.name, p.created_at, "
            "(SELECT COUNT(*) FROM entry e WHERE e.workspace_id=p.workspace_id "
            " AND e.project_id=p.id) AS entries, "
            "(SELECT COUNT(*) FROM pending q WHERE q.workspace_id=p.workspace_id "
            " AND q.project_id=p.id AND q.status='pending') AS pending "
            "FROM project p WHERE p.workspace_id=? ORDER BY p.id",
            (DEFAULT_WORKSPACE,),
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
        row = self.db.query_one(
            "SELECT settings FROM project WHERE workspace_id=? AND id=?", (DEFAULT_WORKSPACE, slug)
        )
        if not row:
            raise SemulaError(404, "project_not_found", f"No project '{slug}'.", param="project")
        return ProjectSettings.model_validate(json.loads(row["settings"]))

    def update_project(
        self, slug: str, name: str | None = None, settings: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Rename and/or patch settings. A ``null`` value resets that setting to its default."""
        before = self.project_settings(slug)
        if name:
            self.db.execute(
                "UPDATE project SET name=? WHERE workspace_id=? AND id=?",
                (name, DEFAULT_WORKSPACE, slug),
            )
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
            self.db.execute(
                "UPDATE project SET settings=? WHERE workspace_id=? AND id=?",
                (after.model_dump_json(exclude_defaults=True), DEFAULT_WORKSPACE, slug),
            )
            if any(getattr(before, f) != getattr(after, f) for f in FINGERPRINT_FIELDS):
                self._rekey(slug, after)
        return self.get_project(slug)

    def _rekey(self, slug: str, settings: ProjectSettings) -> None:
        """Recompute fingerprints after a matching setting changed, from stored requests."""
        config = settings.fingerprint_config()
        for table in ("entry", "pending"):
            rows = self.db.query(
                f"SELECT id, canonical_request FROM {table} WHERE workspace_id=? AND project_id=?",  # noqa: S608
                (DEFAULT_WORKSPACE, slug),
            )
            for row in rows:
                req = CanonicalRequest.from_json(row["canonical_request"])
                self.db.execute(
                    f"UPDATE {table} SET fingerprint=?, exact_fingerprint=? WHERE id=?",  # noqa: S608
                    (
                        normalised_fingerprint(req, config),
                        exact_fingerprint(req, config),
                        row["id"],
                    ),
                )

    def delete_project(self, slug: str) -> None:
        self.require_project(slug)
        if slug == DEFAULT_PROJECT:
            raise SemulaError(409, "default_project", "The default project cannot be deleted.")
        for pid in [
            r["id"]
            for r in self.db.query(
                "SELECT id FROM pending WHERE workspace_id=? AND project_id=?",
                (DEFAULT_WORKSPACE, slug),
            )
        ]:
            event = self._waiters.pop(pid, None)
            if event:
                event.set()  # release held calls; they will find no answer and time out cleanly
        self.db.execute(
            "DELETE FROM call_log WHERE workspace_id=? AND project_id=?", (DEFAULT_WORKSPACE, slug)
        )
        self.db.execute(
            "DELETE FROM project WHERE workspace_id=? AND id=?", (DEFAULT_WORKSPACE, slug)
        )

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
            ("exact", "exact_fingerprint", exact_fingerprint(req, config)),
            ("normalised", "fingerprint", normalised_fingerprint(req, config)),
        ):
            row = self.db.query_one(
                f"SELECT id, responses FROM entry WHERE workspace_id=? AND project_id=? "  # noqa: S608
                f"AND {column}=? ORDER BY created_at LIMIT 1",
                (DEFAULT_WORKSPACE, project, fp),
            )
            if row:
                return Match(row["id"], tier, json.loads(row["responses"])[0])
        return None

    def record_hit(self, entry_id: str) -> None:
        self.db.execute("UPDATE entry SET hit_count = hit_count + 1 WHERE id=?", (entry_id,))

    # --- pending queue ----------------------------------------------------------
    def open_pending(self, req: CanonicalRequest, project: str = DEFAULT_PROJECT) -> str:
        """Park a miss. A second identical miss joins the first instead of duplicating."""
        config = self.project_settings(project).fingerprint_config()
        fp = normalised_fingerprint(req, config)
        row = self.db.query_one(
            "SELECT id FROM pending WHERE workspace_id=? AND project_id=? AND fingerprint=? "
            "AND status='pending'",
            (DEFAULT_WORKSPACE, project, fp),
        )
        if row:
            pid = str(row["id"])
        else:
            pid = new_ulid()
            self.db.execute(
                "INSERT INTO pending (id, workspace_id, project_id, fingerprint, "
                "exact_fingerprint, canonical_request) VALUES (?,?,?,?,?,?)",
                (
                    pid,
                    DEFAULT_WORKSPACE,
                    project,
                    fp,
                    exact_fingerprint(req, config),
                    req.to_json(),
                ),
            )
        self._waiters.setdefault(pid, asyncio.Event())
        return pid

    async def wait_for_answer(self, pending_id: str, timeout: float) -> dict[str, Any] | None:
        """Wait for an answer. Returns the stored response, or None on timeout."""
        event = self._waiters.setdefault(pending_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return None
        row = self.db.query_one("SELECT entry_id FROM pending WHERE id=?", (pending_id,))
        if not row or not row["entry_id"]:
            return None
        entry = self.db.query_one("SELECT responses FROM entry WHERE id=?", (row["entry_id"],))
        return json.loads(entry["responses"])[0] if entry else None

    def list_pending(
        self, status: str | None = "pending", project: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM pending WHERE workspace_id=?"
        params: list[Any] = [DEFAULT_WORKSPACE]
        if status:
            sql += " AND status=?"
            params.append(status)
        if project:
            self.require_project(project)
            sql += " AND project_id=?"
            params.append(project)
        rows = self.db.query(sql + " ORDER BY id", params)
        return [_pending_view(r, full=False) for r in rows]

    def get_pending(self, pending_id: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM pending WHERE id=? AND workspace_id=?", (pending_id, DEFAULT_WORKSPACE)
        )
        if not row:
            raise SemulaError(404, "pending_not_found", f"No pending request {pending_id}.")
        return _pending_view(row, full=True)

    def answer_pending(self, pending_id: str, payload: Any) -> str:
        """Validate an answer, save it as an entry, and release any held calls."""
        row = self.db.query_one(
            "SELECT * FROM pending WHERE id=? AND workspace_id=?", (pending_id, DEFAULT_WORKSPACE)
        )
        if not row:
            raise SemulaError(404, "pending_not_found", f"No pending request {pending_id}.")
        if row["status"] != "pending":
            raise SemulaError(409, "already_answered", f"{pending_id} was already answered.")
        stored = parse_answer(payload)
        validate_tool_calls(stored, CanonicalRequest.from_json(row["canonical_request"]).tools)
        entry_id = new_ulid()
        self.db.execute(
            "INSERT INTO entry (id, workspace_id, project_id, fingerprint, exact_fingerprint, "
            "canonical_request, responses) VALUES (?,?,?,?,?,?,?)",
            (
                entry_id,
                row["workspace_id"],
                row["project_id"],
                row["fingerprint"],
                row["exact_fingerprint"],
                row["canonical_request"],
                json.dumps([stored]),
            ),
        )
        self.db.execute(
            "UPDATE pending SET status='answered', entry_id=?, "
            "answered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (entry_id, pending_id),
        )
        event = self._waiters.pop(pending_id, None)
        if event:
            event.set()
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
            "INSERT INTO call_log (workspace_id, project_id, mode, outcome, tier, latency_ms, "
            "entry_id, pending_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                DEFAULT_WORKSPACE,
                project,
                self.settings.mode,
                outcome,
                tier,
                (time.perf_counter() - started) * 1000,
                entry_id,
                pending_id,
            ),
        )


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
