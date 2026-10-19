"""The ASGI app factory: one process, one port (ADR-0001)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, api_admin, api_openai
from .config import Settings
from .db import Database
from .errors import SemulaError
from .service import Service


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    app = FastAPI(title="semula", version=__version__)
    app.state.settings = settings
    app.state.db = db
    app.state.service = Service(db, settings)

    @app.exception_handler(SemulaError)
    async def _semula_error(_: Request, exc: SemulaError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "mode": settings.mode}

    app.include_router(api_openai.router)
    app.include_router(api_admin.router)
    return app
