"""Headers every response outside the dashboard should carry (SEM-114).

The dashboard (indah) sends its own policy. Everything else semula serves is JSON, or one of the
few pages in ``api_auth``: a response must not be sniffed into another type or kept by a cache
(it can hold a token or a recorded prompt), and the sign-in pages carry their own, stricter set.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

COVERED = ("/v1", "/api", "/mcp", "/auth", "/health")
DEFAULTS = {"x-content-type-options": "nosniff", "cache-control": "no-store"}


class ResponseHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(COVERED):
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in DEFAULTS.items():
                    if name not in headers:  # a page that set its own keeps it
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, with_headers)
