"""Build the dashboard after start-up and serve it at ``/ui``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import Any

from starlette.types import Receive, Scope, Send

from ..schema import DEFAULT_PROJECT
from ..service import Service

log = logging.getLogger("semula.dashboard")

READY_TIMEOUT = 30.0
_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>semula</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:4rem auto}}</style>
<h1>semula</h1><p>{message}</p>"""


async def _plain(send: Send, status: int, message: str) -> None:
    body = _PAGE.format(message=message).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _redirect(send: Send, location: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 307,
            "headers": [(b"location", location.encode()), (b"content-length", b"0")],
        }
    )
    await send({"type": "http.response.body", "body": b""})


class Locked:
    """``/ui`` while ``SEMULA_API_KEY`` is set. A browser cannot send the key on a page load,
    and an open dashboard beside a protected API would let anyone answer calls and edit
    entries, so it is switched off until the hosted sign-in exists (ADR-0010)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await _plain(
                send,
                403,
                "The dashboard is off because <code>SEMULA_API_KEY</code> is set. "
                "The API, the CLI and MCP work with the key.",
            )


class LazyDashboard:
    """The indah app, built on the first request to ``/ui``.

    Not at start-up: importing and building indah costs a few tenths of a second, and the
    container's cold start budget (2 s, measured about 1.9 s on CI) has no room for it. The
    first visit pays that instead, in a thread so the event loop keeps serving. Building also
    starts the bridge that republishes the service's call events on an indah ``Topic`` so every
    open tab follows the log; ``stop`` ends it with the server.
    """

    def __init__(self, service: Service) -> None:
        self._service = service
        self._app: Any = None
        self._bridge_task: asyncio.Task[None] | None = None
        self._built = asyncio.Event()
        self._build_lock = threading.Lock()

    def _build(self) -> Any:
        with self._build_lock:
            if self._app is None:
                from .app import build_dashboard

                self._app = build_dashboard(self._service)
        return self._app

    def _landing_project(self) -> str:
        """Where ``/ui`` opens: the project of the latest call, so a first look shows traffic
        rather than an empty ``default``."""
        latest = self._service.list_calls(limit=1)
        return str(latest[0]["project"]) if latest else DEFAULT_PROJECT

    async def _ensure(self) -> Any:
        if self._bridge_task is None:
            # Listen before building: the build seeds the log from the database, and a call
            # logged meanwhile must not fall between that and the live feed (the dashboard
            # drops what the seed already holds).
            self._bridge_task = asyncio.create_task(self._bridge(self._service.events.subscribe()))
        if self._app is None:
            try:
                await asyncio.to_thread(self._build)
            except Exception:
                log.exception("The dashboard failed to start.")
                return None
            self._built.set()
        return self._app

    async def _bridge(self, sub: Any) -> None:
        with sub:
            await self._built.wait()
            while True:
                self._app.state.publish(await sub.get())

    async def stop(self) -> None:
        if self._bridge_task:
            self._bridge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bridge_task

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == scope.get("root_path", ""):
            project = await asyncio.to_thread(self._landing_project)
            await _redirect(send, f"{scope.get('root_path', '')}/projects/{project}/log")
            return
        app = await self._ensure()
        if app is None:
            if scope["type"] == "http":
                await _plain(send, 503, "The dashboard is not ready.")
            return
        await app(scope, receive, send)
