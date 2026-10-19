"""The ASGI app factory: one process, one port (ADR-0001)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, api_admin, api_openai
from .auth import ApiKeyMiddleware
from .config import Settings
from .db import Database
from .errors import SemulaError
from .mcp_server import build_mcp
from .service import Service


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.resolved_database_url)
    service = Service(db, settings)
    service.sync_index()
    mcp_app = build_mcp(service).http_app(path="/")
    app = FastAPI(title="semula", version=__version__, lifespan=mcp_app.lifespan)
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
    app.mount("/mcp", mcp_app)
    if settings.api_key:
        app.add_middleware(ApiKeyMiddleware, key=settings.api_key)
    return app
