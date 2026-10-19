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


@router.get("/projects")
def list_projects(request: Request) -> dict[str, Any]:
    return {"projects": _service(request).list_projects()}


@router.post("/projects", status_code=201)
async def create_project(request: Request) -> dict[str, Any]:
    body = await _json(request)
    return _service(request).create_project(str(body.get("id", "")), body.get("name"))


@router.get("/projects/{project_id}")
def get_project(project_id: str, request: Request) -> dict[str, Any]:
    return _service(request).get_project(project_id)


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, request: Request) -> dict[str, Any]:
    body = await _json(request)
    settings = body.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise SemulaError(400, "invalid_request", "'settings' must be an object.", param="settings")
    return _service(request).update_project(project_id, body.get("name"), settings)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, request: Request) -> None:
    _service(request).delete_project(project_id)


def _actor(request: Request) -> str:
    """Who is writing. Anything but an explicit 'human' is treated as an agent (ADR-0005)."""
    return "human" if request.headers.get("x-semula-actor", "").lower() == "human" else "agent"


@router.get("/projects/{project_id}/entries")
def list_entries(
    project_id: str, request: Request, q: str | None = None, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    return _service(request).list_entries(project_id, q, limit, offset)


@router.get("/entries/{entry_id}")
def get_entry(entry_id: str, request: Request) -> dict[str, Any]:
    return _service(request).get_entry(entry_id)


@router.put("/entries/{entry_id}")
async def update_entry(entry_id: str, request: Request) -> dict[str, Any]:
    body = await _json(request)
    version = body.pop("version", None)
    force = bool(body.pop("force", False))
    return _service(request).update_entry(
        entry_id, version, body, actor=_actor(request), force=force
    )


@router.delete("/entries/{entry_id}", status_code=204)
def delete_entry(
    entry_id: str, request: Request, version: int | None = None, force: bool = False
) -> None:
    _service(request).delete_entry(entry_id, version, actor=_actor(request), force=force)


@router.get("/pending")
def list_pending(
    request: Request, status: str | None = "pending", project: str | None = None
) -> dict[str, Any]:
    return {"pending": _service(request).list_pending(None if status == "all" else status, project)}


@router.get("/pending/{pending_id}")
def get_pending(pending_id: str, request: Request) -> dict[str, Any]:
    return _service(request).get_pending(pending_id)


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise SemulaError(400, "invalid_json", "The request body is not valid JSON.") from exc
    if not isinstance(body, dict):
        raise SemulaError(400, "invalid_request", "The body must be a JSON object.")
    return body


@router.post("/pending/{pending_id}/answer")
async def answer_pending(pending_id: str, request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise SemulaError(400, "invalid_json", "The request body is not valid JSON.") from exc
    entry_id = _service(request).answer_pending(pending_id, payload)
    return {"entry_id": entry_id, "pending_id": pending_id, "status": "answered"}
