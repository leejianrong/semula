"""The ASGI app factory: one process, one port (ADR-0001)."""

from __future__ import annotations

from fastapi import FastAPI

from . import __version__
from .config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="semula", version=__version__)
    app.state.settings = settings

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "mode": settings.mode}

    return app
