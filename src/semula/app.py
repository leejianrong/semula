"""The ASGI app factory: one process, one port (ADR-0001)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Receive, Scope, Send

from . import __version__, api_admin, api_openai
from .auth import ApiKeyMiddleware
from .config import Settings
from .db import Database
from .embeddings import Embedder
from .errors import SemulaError
from .service import Service

MCP_READY_TIMEOUT = 30.0


class LazyMcp:
    """The MCP app at ``/mcp``, built after startup.

    Importing fastmcp is the largest single cost of starting the server (about 0.8 s) and
    replay never needs it. So the server comes up first, and this builds the MCP app in the
    background; a request to ``/mcp`` waits for it. One task enters and leaves the MCP app's
    lifespan, as its task group requires.
    """

    def __init__(self, service: Service) -> None:
        self._service = service
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._app: Any = None
        self._started = False

    def _build(self) -> Any:
        from .mcp_server import build_mcp

        return build_mcp(self._service).http_app(path="/")

    async def run(self, parent: FastAPI) -> None:
        self._started = True
        try:
            app = await asyncio.to_thread(self._build)
            async with app.lifespan(parent):
                self._app = app
                self._ready.set()
                await self._stop.wait()
        except Exception:  # leave /mcp answering 503 rather than hanging
            logging.getLogger("semula").exception("The MCP endpoint failed to start.")
        finally:
            self._ready.set()

    async def stop(self) -> None:
        self._stop.set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._started:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._ready.wait(), MCP_READY_TIMEOUT)
        if self._app is None:
            body = json.dumps(
                SemulaError(
                    503,
                    "mcp_unavailable",
                    "The MCP endpoint is not ready.",
                    err_type="server_error",
                ).body()
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)


def create_app(settings: Settings | None = None, embedder: Embedder | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.resolved_database_url)
    service = Service(db, settings, embedder)
    service.sync_index()
    service.sync_embeddings()
    mcp = LazyMcp(service)

    @asynccontextmanager
    async def lifespan(parent: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(mcp.run(parent))
        try:
            yield
        finally:
            await mcp.stop()
            await task

    app = FastAPI(title="semula", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.service = service

    @app.exception_handler(SemulaError)
    async def _semula_error(_: Request, exc: SemulaError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(
            SemulaError(exc.status_code, code, str(exc.detail)).body(), status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        param = ".".join(str(p) for p in first.get("loc", ()))
        message = first.get("msg", "Invalid request.")
        return JSONResponse(
            SemulaError(422, "invalid_request", message, param=param or None).body(),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        error = SemulaError(
            500, "internal_error", "Unexpected server error.", err_type="server_error"
        )
        return JSONResponse(error.body(), status_code=500)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "mode": app.state.service.mode}

    app.include_router(api_openai.router)
    app.include_router(api_admin.router)
    app.mount("/mcp", mcp)
    if settings.api_key:
        app.add_middleware(ApiKeyMiddleware, key=settings.api_key)
    return app
