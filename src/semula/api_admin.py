"""The admin REST API under /api/v1 (ADR-0005). Thin: every route calls the service."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from functools import partial
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool as offload

from . import canonical
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
    return await offload(
        _service(request).create_project, str(body.get("id", "")), body.get("name")
    )


@router.get("/projects/{project_id}")
def get_project(project_id: str, request: Request) -> dict[str, Any]:
    return _service(request).get_project(project_id)


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, request: Request) -> dict[str, Any]:
    body = await _json(request)
    settings = body.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise SemulaError(400, "invalid_request", "'settings' must be an object.", param="settings")
    return await offload(_service(request).update_project, project_id, body.get("name"), settings)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, request: Request) -> None:
    _service(request).delete_project(project_id)


def _actor(request: Request) -> str:
    """Who is writing. Anything but an explicit 'human' is treated as an agent (ADR-0005)."""
    return "human" if request.headers.get("x-semula-actor", "").lower() == "human" else "agent"


@router.get("/projects/{project_id}/entries")
def list_entries(
    project_id: str,
    request: Request,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    pinned: bool | None = None,
    has_alias: bool | None = None,
    tool_calls: bool | None = None,
    never_hit: bool | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> dict[str, Any]:
    """Entries without their bodies. Filters (``pinned``, ``has_alias``, ``tool_calls``,
    ``never_hit``) combine with AND; ``sort`` is ``hits``, ``updated`` or ``created``."""
    return _service(request).list_entries(
        project_id,
        q,
        limit,
        offset,
        pinned=pinned,
        has_alias=has_alias,
        tool_calls=tool_calls,
        never_hit=never_hit,
        sort=sort,
        order=order,
    )


@router.post("/projects/{project_id}/entries")
async def upsert_entry(project_id: str, request: Request, response: Response) -> dict[str, Any]:
    """Body: ``{"request": {chat body}, "response": answer, "version"?: n, "force"?: bool}``."""
    body = await _json(request)
    if not isinstance(body.get("request"), dict) or "response" not in body:
        raise SemulaError(
            400, "invalid_request", "Send {'request': {...}, 'response': ...}.", param="request"
        )
    entry, created = await offload(
        partial(
            _service(request).upsert_entry,
            project_id,
            body["request"],
            body["response"],
            version=body.get("version"),
            actor=_actor(request),
            force=bool(body.get("force", False)),
        )
    )
    response.status_code = 201 if created else 200
    return entry


@router.get("/entries/{entry_id}")
def get_entry(entry_id: str, request: Request) -> dict[str, Any]:
    return _service(request).get_entry(entry_id)


@router.put("/entries/{entry_id}")
async def update_entry(entry_id: str, request: Request) -> dict[str, Any]:
    body = await _json(request)
    version = body.pop("version", None)
    force = bool(body.pop("force", False))
    return await offload(
        partial(
            _service(request).update_entry,
            entry_id,
            version,
            body,
            actor=_actor(request),
            force=force,
        )
    )


@router.delete("/entries/{entry_id}", status_code=204)
def delete_entry(
    entry_id: str, request: Request, version: int | None = None, force: bool = False
) -> None:
    _service(request).delete_entry(entry_id, version, actor=_actor(request), force=force)


@router.post("/projects/{project_id}/test-match")
async def test_match(project_id: str, request: Request) -> dict[str, Any]:
    """Body: ``{"prompt": "text"}``, or ``{"request": {chat completion body}}``."""
    body = await _json(request)
    if isinstance(body.get("request"), dict):
        chat_body = body["request"]
    elif isinstance(body.get("prompt"), str):
        chat_body = {"messages": [{"role": "user", "content": body["prompt"]}]}
    else:
        raise SemulaError(
            400, "invalid_request", "Send {'prompt': 'text'} or {'request': {...}}.", param="prompt"
        )
    if not isinstance(chat_body.get("messages"), list):
        raise SemulaError(
            400, "invalid_request", "The request needs a 'messages' list.", param="request"
        )
    return await offload(_service(request).test_match, canonical.from_openai(chat_body), project_id)


@router.get("/mode")
def get_mode(request: Request) -> dict[str, str]:
    return {"mode": _service(request).mode}


@router.put("/mode")
async def put_mode(request: Request) -> dict[str, str]:
    body = await _json(request)
    return {"mode": await offload(_service(request).set_mode, str(body.get("mode", "")))}


@router.get("/pending")
def list_pending(
    request: Request, status: str | None = "pending", project: str | None = None
) -> dict[str, Any]:
    return {"pending": _service(request).list_pending(None if status == "all" else status, project)}


@router.get("/pending/{pending_id}")
def get_pending(pending_id: str, request: Request) -> dict[str, Any]:
    return _service(request).get_pending(pending_id)


@router.get("/embeddings")
def embeddings_status(request: Request) -> dict[str, Any]:
    """Whether semantic matching is on, the model, and how many entries have vectors yet."""
    return _service(request).embeddings_status()


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, request: Request) -> dict[str, Any]:
    """A ``semula/1`` bundle of the project's entries and approved aliases."""
    return _service(request).export_project(project_id)


@router.post("/import")
async def import_project(request: Request) -> dict[str, Any]:
    """Body: ``{"bundle": {...}, "project"?: id, "force"?: bool}``. Idempotent."""
    body = await _json(request)
    if "bundle" not in body:
        raise SemulaError(400, "invalid_request", "Send {'bundle': {...}}.", param="bundle")
    project = body.get("project")
    if project is not None and not isinstance(project, str):
        raise SemulaError(400, "invalid_request", "'project' must be a string.", param="project")
    return await offload(
        partial(
            _service(request).import_project,
            body["bundle"],
            project,
            force=bool(body.get("force", False)),
        )
    )


@router.get("/log")
def list_calls(
    request: Request,
    project: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> dict[str, Any]:
    """Recent calls, newest first (retention applies: 30 days or 50k rows by default)."""
    return {"calls": _service(request).list_calls(project, outcome, limit, before_id)}


@router.get("/log/stream")
async def log_stream(request: Request, project: str | None = None) -> StreamingResponse:
    """Server-sent events: one ``call`` event per call as it is logged. A ``: connected``
    comment marks the moment the subscription is live, ``: keepalive`` every 15 s."""
    service = _service(request)
    if project:
        await offload(service.require_project, project)

    async def events() -> AsyncIterator[str]:
        with service.events.subscribe() as sub:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(sub.get(), 15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if project and event.get("project") != project:
                    continue
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/candidates")
def list_candidates(
    request: Request, status: str | None = "open", project: str | None = None
) -> dict[str, Any]:
    """Weak lexical matches waiting for a decision, best score first. ``status=all`` shows
    decided ones too."""
    found = _service(request).list_candidates(project, None if status == "all" else status)
    return {"candidates": found}


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, request: Request) -> dict[str, Any]:
    return _service(request).get_candidate(candidate_id)


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate(candidate_id: str, request: Request) -> dict[str, Any]:
    return await offload(_service(request).approve_candidate, candidate_id)


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(candidate_id: str, request: Request) -> dict[str, Any]:
    return await offload(_service(request).reject_candidate, candidate_id)


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
    entry_id = await offload(_service(request).answer_pending, pending_id, payload)
    return {"entry_id": entry_id, "pending_id": pending_id, "status": "answered"}
