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


LOCAL = Scope()


def scope_of(request: Request) -> Scope:
    """The scope the request runs in. Locally it is always the local workspace; the credential
    middleware of the hosted build sets ``request.state.scope`` before any route runs."""
    found = getattr(request.state, "scope", None)
    return found if isinstance(found, Scope) else LOCAL
