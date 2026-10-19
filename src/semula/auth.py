"""Optional shared-secret check for every surface (SEM-28).

Local use is open by default. If ``SEMULA_API_KEY`` is set, ``/v1``, ``/api`` and
``/mcp`` require it as ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``.
``/health`` stays open for container health checks. This is a shared secret, not
users or roles; hosted auth is a separate design (see the ADRs).
"""

from __future__ import annotations

import hmac
import json

from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import SemulaError

PROTECTED_PREFIXES = ("/v1", "/api", "/mcp")


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp, key: str) -> None:
        self.app = app
        self._key = key.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not scope["path"].startswith(
            PROTECTED_PREFIXES
        ):
            await self.app(scope, receive, send)
            return
        if self._authorised(scope):
            await self.app(scope, receive, send)
            return
        error = SemulaError(
            401,
            "invalid_api_key",
            "Missing or wrong API key. Send 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
            err_type="authentication_error",
        )
        body = json.dumps(error.body()).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _authorised(self, scope: Scope) -> bool:
        headers = {k.lower(): v for k, v in scope["headers"]}
        supplied = headers.get(b"x-api-key", b"")
        auth = headers.get(b"authorization", b"")
        if auth[:7].lower() == b"bearer ":
            supplied = supplied or auth[7:]
        return hmac.compare_digest(supplied.strip(), self._key)
