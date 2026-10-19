"""The admin REST API under /api/v1 (ADR-0005). Thin: every route calls the service."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .errors import SemulaError
from .service import Service

router = APIRouter(prefix="/api/v1")


def _service(request: Request) -> Service:
    service: Service = request.app.state.service
    return service


@router.get("/pending")
def list_pending(request: Request, status: str | None = "pending") -> dict[str, Any]:
    return {"pending": _service(request).list_pending(None if status == "all" else status)}


@router.get("/pending/{pending_id}")
def get_pending(pending_id: str, request: Request) -> dict[str, Any]:
    return _service(request).get_pending(pending_id)


@router.post("/pending/{pending_id}/answer")
async def answer_pending(pending_id: str, request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise SemulaError(400, "invalid_json", "The request body is not valid JSON.") from exc
    entry_id = _service(request).answer_pending(pending_id, payload)
    return {"entry_id": entry_id, "pending_id": pending_id, "status": "answered"}
