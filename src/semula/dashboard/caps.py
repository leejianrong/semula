"""A cap on the dashboard tabs one person has open (SEM-115).

A tab is one open event stream. indah ends a session only when it is discarded or reaped, not when
the browser goes away, so counting sessions would lock a person out after they had opened and
closed a few tabs; counting streams does not, because a stream ends when its tab does.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

if TYPE_CHECKING:
    from ..service import Service

STREAM = "/ui/api/stream"


class StreamCap:
    def __init__(self, app: ASGIApp, service: Service, cookie: str, cap: int) -> None:
        self.app = app
        self.service = service
        self.cookie = cookie
        self.cap = cap
        self._open: dict[str, int] = {}
        self._lock = threading.Lock()

    def _secret(self, scope: Scope) -> str:
        for name, value in scope["headers"]:
            if name == b"cookie":
                for part in value.decode("latin-1").split(";"):
                    key, _, val = part.strip().partition("=")
                    if key == self.cookie:
                        return str(val)
        return ""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != STREAM:
            await self.app(scope, receive, send)
            return
        secret = self._secret(scope)
        session = (
            await run_in_threadpool(self.service.authenticate_session, secret) if secret else None
        )
        if session is None:  # not signed in: the dashboard's own check answers
            await self.app(scope, receive, send)
            return
        person = str(session["person_id"])
        with self._lock:
            if self._open.get(person, 0) >= self.cap:
                full = True
            else:
                full = False
                self._open[person] = self._open.get(person, 0) + 1
        if full:
            refusal = PlainTextResponse(
                "Too many open tabs. Close one, then reload.",
                status_code=429,
                headers={"Retry-After": "10"},
            )
            await refusal(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            with self._lock:
                self._open[person] -= 1
                if self._open[person] <= 0:
                    del self._open[person]
