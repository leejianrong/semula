"""Optional shared-secret check for every surface (SEM-28).

Local use is open by default. If ``SEMULA_API_KEY`` is set, ``/v1``, ``/api`` and
``/mcp`` require it as ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``.
``/health`` stays open for container health checks. This is a shared secret, not
users or roles; hosted auth is a separate design (see the ADRs).
"""

from __future__ import annotations

import hmac
import json
import threading
import time
from collections import deque
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Send
from starlette.types import Scope as ASGIScope

from .errors import SemulaError
from .permissions import PEOPLE_ONLY, PROJECT_IN_PATH, allows, required_scope
from .scope import Principal, Scope

if TYPE_CHECKING:
    from .service import Service

PROTECTED_PREFIXES = ("/v1", "/api", "/mcp")


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp, key: str) -> None:
        self.app = app
        self._key = key.encode()

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
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

    def _authorised(self, scope: ASGIScope) -> bool:
        headers = {k.lower(): v for k, v in scope["headers"]}
        supplied = headers.get(b"x-api-key", b"")
        auth = headers.get(b"authorization", b"")
        if auth[:7].lower() == b"bearer ":
            supplied = supplied or auth[7:]
        return hmac.compare_digest(supplied.strip(), self._key)


class AttemptLimiter:
    """Failed credentials per client address in a sliding window, so token guessing is slow and
    the cheap 401 path cannot be turned into load (ADR-0010 section 5)."""

    def __init__(self, limit: int = 20, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, client: str) -> float:
        """Seconds until ``client`` may try again, or 0."""
        now = time.monotonic()
        with self._lock:
            recent = self._failures.get(client)
            if not recent:
                return 0.0
            while recent and now - recent[0] > self.window:
                recent.popleft()
            if len(recent) >= self.limit:
                return self.window - (now - recent[0])
            return 0.0

    def failed(self, client: str) -> None:
        with self._lock:
            self._failures.setdefault(client, deque()).append(time.monotonic())


class TokenAuthMiddleware:
    """Hosted mode: every ``/v1``, ``/api`` and ``/mcp`` call carries an API token (ADR-0010).

    The token names the workspace, the scope and optionally one project; this puts the resulting
    ``Principal`` and ``Scope`` where the routes read them, and refuses a call the scope does not
    allow. ``/health`` stays open. A project-bound token that names no project is given its own;
    one that names another project gets a 404, as if the project did not exist. What a bound
    token may see through routes that name no project (the pending list, an entry by id) is
    narrowed by the service itself.
    """

    def __init__(
        self, app: ASGIApp, service: Service, limiter: AttemptLimiter | None = None
    ) -> None:
        self.app = app
        self.service = service
        self.limiter = limiter or AttemptLimiter()

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(PROTECTED_PREFIXES):
            await self.app(scope, receive, send)
            return
        client = (scope.get("client") or ("", 0))[0] or "unknown"
        wait = self.limiter.blocked(client)
        if wait:
            await self._refuse(
                send,
                SemulaError(
                    429, "too_many_attempts", "Too many failed attempts. Try again shortly.",
                    err_type="authentication_error",
                ),
                [(b"retry-after", str(int(wait) + 1).encode())],
            )  # fmt: skip
            return
        headers = {k.lower(): v for k, v in scope["headers"]}
        auth = headers.get(b"authorization", b"")
        secret = auth[7:].strip().decode() if auth[:7].lower() == b"bearer " else ""
        secret = secret or headers.get(b"x-api-key", b"").decode().strip()
        principal = (
            await run_in_threadpool(self.service.authenticate_token, secret, client)
            if secret
            else None
        )
        if principal is None:
            self.limiter.failed(client)
            await self._refuse(
                send,
                SemulaError(
                    401, "invalid_api_key", "Missing or invalid API token. Send "
                    "'Authorization: Bearer smla_live_...'.", err_type="authentication_error",
                ),
                [(b"www-authenticate", b"Bearer")],
            )  # fmt: skip
            return
        error = self._check(scope, principal)
        if error:
            await self._refuse(send, error)
            return
        state = scope.setdefault("state", {})
        state["principal"] = principal
        state["scope"] = Scope(principal.workspace_id, principal)
        if principal.project and not self._named_project(scope):
            scope["headers"] = [
                *scope["headers"],
                (b"x-semula-project", principal.project.encode()),
            ]
        await self.app(scope, receive, send)

    def _check(self, scope: ASGIScope, principal: Principal) -> SemulaError | None:
        path, method = scope["path"], scope["method"]
        if path.startswith("/mcp") and any(k.lower() == b"origin" for k, _ in scope["headers"]):
            return SemulaError(  # a browser page calling MCP (DNS rebinding); clients send none
                403, "origin_not_allowed", "MCP does not accept browser requests.",
                err_type="authentication_error",
            )  # fmt: skip
        if PEOPLE_ONLY.fullmatch(path.rstrip("/")):
            return SemulaError(
                403, "forbidden", "Tokens cannot manage tokens.", err_type="authentication_error"
            )
        needed = required_scope(method, path)
        if not allows(principal.scope, needed):
            return SemulaError(
                403, "insufficient_scope",
                f"This token has scope '{principal.scope}'; this call needs '{needed}'."
                if needed
                else "No token may call this route.",
                err_type="authentication_error",
            )  # fmt: skip
        if principal.project:
            named = self._named_project(scope)
            if named and named != principal.project:
                return SemulaError(
                    404, "project_not_found", f"No project '{named}'.", param="project"
                )
        return None

    @staticmethod
    def _named_project(scope: ASGIScope) -> str | None:
        found = PROJECT_IN_PATH.match(scope["path"])
        if found:
            return found.group(1)
        for key, value in scope["headers"]:
            if key.lower() == b"x-semula-project":
                return str(value.decode())
        return None

    @staticmethod
    async def _refuse(
        send: Send, error: SemulaError, extra: list[tuple[bytes, bytes]] | None = None
    ) -> None:
        body = json.dumps(error.body()).encode()
        await send(
            {
                "type": "http.response.start",
                "status": error.status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *(extra or []),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
