"""Who a dashboard request is, when the server is hosted (ADR-0010).

The session cookie made at sign-in names a person; their membership says which workspace they
are working in and in which role. indah is told through an authenticator, and a policy maps the
three roles to what the screens ask for with ``ctx.can``: ``view`` (everyone who belongs), ``edit``
(editors and admins) and ``manage`` (admins). Nothing here trusts the browser: the cookie is
looked up on every request, so a revoked sign-in or a removed member fails on the next click.
"""

from __future__ import annotations

import asyncio
from typing import Any

import indah

from ..service import Service

POLICY = indah.Policy(
    {"viewer": ["view"], "editor": ["view", "edit"], "admin": ["*"]},
)


def authenticator(service: Service, cookie: str) -> Any:
    """An indah authenticator reading the session cookie named ``cookie``."""

    async def who(request: Any) -> indah.Principal | None:
        secret = request.cookies.get(cookie, "")
        session = await asyncio.to_thread(service.authenticate_session, secret)
        if session is None:
            return None
        found = await asyncio.to_thread(service.principal_for_session, session)
        return indah.Principal(
            id=session["person_id"],
            name=session["name"],
            email=session["email"],
            roles=frozenset({found.role}) if found and found.role else frozenset(),
            workspace=found.workspace_id if found else None,
            claims={"session_id": session["session_id"], "csrf": session["csrf"]},
        )

    return who
