"""The OpenAI-compatible surface under /v1."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import canonical
from .db import DEFAULT_PROJECT
from .errors import SemulaError
from .responses import build_completion
from .service import Service
from .streaming import sse_events
from .templating import expand_stored

router = APIRouter(prefix="/v1")


def _project(request: Request, service: Service) -> str:
    """Path beats header beats default. An unknown project is a 404, never a silent fallback."""
    slug = (
        request.path_params.get("project_id")
        or request.headers.get("x-semula-project")
        or DEFAULT_PROJECT
    )
    return service.require_project(str(slug))


@router.post("/chat/completions", response_model=None)
@router.post("/projects/{project_id}/chat/completions", response_model=None)
async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
    service: Service = request.app.state.service
    project = _project(request, service)
    started = time.perf_counter()
    body = await _read_body(request)
    model = str(body.get("model") or "semula")
    req = canonical.from_openai(body)

    match = service.find(req, project)
    if match:
        service.record_hit(match.entry_id)
        service.log_call("hit", started, tier=match.tier, entry_id=match.entry_id, project=project)
        return _respond(service, body, match.stored, model)

    if service.settings.mode == "replay":
        service.log_call("miss", started, project=project)
        raise SemulaError(
            424,
            "semula_no_match",
            "No recorded answer matches this request, and semula is in replay mode.",
            err_type="semula_error",
        )

    pending_id = service.open_pending(req, project)
    stored = await service.wait_for_answer(pending_id, service.record_timeout(project))
    if stored is None:
        service.log_call("record_timeout", started, pending_id=pending_id, project=project)
        raise SemulaError(
            504,
            "semula_record_timeout",
            f"No agent answered pending request {pending_id} within "
            f"{service.record_timeout(project):g}s. It stays pending; "
            "a late answer is still saved.",
            err_type="semula_error",
        )
    service.log_call("recorded", started, pending_id=pending_id, project=project)
    return _respond(service, body, stored, model)


def _respond(
    service: Service, body: dict[str, Any], stored: dict[str, Any], model: str
) -> JSONResponse | StreamingResponse:
    stored = expand_stored(stored, model)
    if not body.get("stream"):
        return JSONResponse(build_completion(stored, model))
    options = body.get("stream_options")
    include_usage = bool(isinstance(options, dict) and options.get("include_usage"))
    events = sse_events(
        stored, model, chunk_size=service.settings.chunk_size, include_usage=include_usage
    )
    return StreamingResponse(events, media_type="text/event-stream")


def _model(model_id: str) -> dict[str, Any]:
    return {"id": model_id, "object": "model", "created": 0, "owned_by": "semula"}


@router.get("/models")
@router.get("/projects/{project_id}/models")
async def models(request: Request) -> dict[str, Any]:
    service: Service = request.app.state.service
    project = _project(request, service)
    return {"object": "list", "data": [_model(m) for m in service.list_models(project)]}


@router.get("/models/{model_id:path}")
@router.get("/projects/{project_id}/models/{model_id:path}")
async def retrieve_model(model_id: str, request: Request) -> dict[str, Any]:
    service: Service = request.app.state.service
    if model_id not in service.list_models(_project(request, service)):
        raise SemulaError(
            404,
            "model_not_found",
            f"The model '{model_id}' does not exist.",
            param="model",
        )
    return _model(model_id)


async def _read_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise SemulaError(400, "invalid_json", "The request body is not valid JSON.") from exc
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise SemulaError(
            400,
            "invalid_request",
            "The body must be an object with a 'messages' list.",
            param="messages",
        )
    return body
