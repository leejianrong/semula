"""The service layer: the only code that reads or writes the database (ADR-0005)."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from .canonical import (
    CanonicalRequest,
    FingerprintConfig,
    exact_fingerprint,
    normalised_fingerprint,
)
from .config import Settings
from .db import DEFAULT_PROJECT, DEFAULT_WORKSPACE, Database
from .errors import SemulaError
from .ids import new_ulid
from .responses import parse_answer, validate_tool_calls


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
        self._config = FingerprintConfig()

    # --- models -----------------------------------------------------------------
    def list_models(self, project: str = DEFAULT_PROJECT) -> list[str]:
        """Model ids for /v1/models: the project ``models`` setting, else the env default."""
        row = self.db.query_one(
            "SELECT settings FROM project WHERE workspace_id=? AND id=?",
            (DEFAULT_WORKSPACE, project),
        )
        configured = json.loads(row["settings"]).get("models") if row else None
        if isinstance(configured, list) and configured:
            return [str(m) for m in configured]
        return list(self.settings.models)

    # --- matching -------------------------------------------------------------
    def find(self, req: CanonicalRequest, project: str = DEFAULT_PROJECT) -> Match | None:
        for tier, column, fp in (
            ("exact", "exact_fingerprint", exact_fingerprint(req, self._config)),
            ("normalised", "fingerprint", normalised_fingerprint(req, self._config)),
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
        fp = normalised_fingerprint(req, self._config)
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
                    exact_fingerprint(req, self._config),
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

    def list_pending(self, status: str | None = "pending") -> list[dict[str, Any]]:
        sql = "SELECT * FROM pending WHERE workspace_id=?"
        params: list[Any] = [DEFAULT_WORKSPACE]
        if status:
            sql += " AND status=?"
            params.append(status)
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
