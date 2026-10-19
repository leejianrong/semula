"""The OpenAI-compatible surface under /v1."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import canonical
from .errors import SemulaError
from .responses import build_completion
from .service import Service
from .streaming import sse_events

router = APIRouter(prefix="/v1")


@router.post("/chat/completions", response_model=None)
async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
    service: Service = request.app.state.service
    started = time.perf_counter()
    body = await _read_body(request)
    model = str(body.get("model") or "semula")
    req = canonical.from_openai(body)

    match = service.find(req)
    if match:
        service.record_hit(match.entry_id)
        service.log_call("hit", started, tier=match.tier, entry_id=match.entry_id)
        return _respond(service, body, match.stored, model)

    if service.settings.mode == "replay":
        service.log_call("miss", started)
        raise SemulaError(
            424,
            "semula_no_match",
            "No recorded answer matches this request, and semula is in replay mode.",
            err_type="semula_error",
        )

    pending_id = service.open_pending(req)
    stored = await service.wait_for_answer(pending_id, service.settings.record_timeout)
    if stored is None:
        service.log_call("record_timeout", started, pending_id=pending_id)
        raise SemulaError(
            504,
            "semula_record_timeout",
            f"No agent answered pending request {pending_id} within "
            f"{service.settings.record_timeout:g}s. It stays pending; "
            "a late answer is still saved.",
            err_type="semula_error",
        )
    service.log_call("recorded", started, pending_id=pending_id)
    return _respond(service, body, stored, model)


def _respond(
    service: Service, body: dict[str, Any], stored: dict[str, Any], model: str
) -> JSONResponse | StreamingResponse:
    if not body.get("stream"):
        return JSONResponse(build_completion(stored, model))
    options = body.get("stream_options")
    include_usage = bool(isinstance(options, dict) and options.get("include_usage"))
    events = sse_events(
        stored, model, chunk_size=service.settings.chunk_size, include_usage=include_usage
    )
    return StreamingResponse(events, media_type="text/event-stream")


@router.get("/models")
async def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": "semula", "object": "model", "owned_by": "semula"}]}


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
