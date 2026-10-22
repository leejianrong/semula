"""Who a request is for: the workspace whose data it may touch (ADR-0011).

The service is bound to one workspace per request with ``Service.for_scope``; every query is
filtered by that workspace, and an id that belongs to another one is simply not found.
Locally there is one workspace, ``default``, and nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .schema import DEFAULT_WORKSPACE

if TYPE_CHECKING:
    from starlette.requests import Request


@dataclass(frozen=True)
class Scope:
    workspace_id: str = DEFAULT_WORKSPACE
    principal: Principal | None = None


LOCAL = Scope()


def scope_of(request: Request) -> Scope:
    """The scope the request runs in. Locally it is always the local workspace; the credential
    middleware of the hosted build sets ``request.state.scope`` before any route runs."""
    found = getattr(request.state, "scope", None)
    return found if isinstance(found, Scope) else LOCAL


@dataclass(frozen=True)
class Principal:
    """Who is acting. ``local`` is the open local mode; a ``token`` belongs to a program (an
    agent); a ``person`` signs in (SEM-84). The actor is derived from this, never from a header,
    once credentials are in use (ADR-0010)."""

    kind: str  # local | token | person
    id: str = "local"
    workspace_id: str = DEFAULT_WORKSPACE
    scope: str | None = None  # a token's scope: replay | record | admin
    project: str | None = None  # a token's project, or None for all of them
    role: str | None = None  # a person's role: viewer | editor | admin
    name: str = ""

    @property
    def actor(self) -> str | None:
        """``agent`` for a token, ``human`` for a person, None locally (the header decides)."""
        return {"token": "agent", "person": "human"}.get(self.kind)


LOCAL_PRINCIPAL = Principal(kind="local")
