"""Roles for people in a workspace (ADR-0010 section 3).

viewer reads. editor also does what recording and review need: answer, edit, approve and reject,
``test_match``, switch a project's mode. admin also does what a human decides: force past a pin,
delete, import and export, project settings, create and delete projects, tokens, members, the
workspace. A role applies to every project in the workspace.

``@needs(role)`` is on each public service method that a person reaches. It does nothing for local
use or a token (a token has its own scopes); for a person it refuses, with 403, a role that is too
low before the method looks at anything. ``UNRESTRICTED`` lists the methods that are not about a
person's access (serving, maintenance, sign-in itself); a test fails for any public method that is
in neither place.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, TypeVar

ROLES = ("viewer", "editor", "admin")
LEVEL = {name: i for i, name in enumerate(ROLES, start=1)}

F = TypeVar("F", bound=Callable[..., Any])


def needs(role: str) -> Callable[[F], F]:
    if role not in LEVEL:
        raise ValueError(f"unknown role {role!r}")

    def decorate(fn: F) -> F:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
                self._require_role(role)
                return await fn(self, *args, **kwargs)

            async_guarded.needs = role  # type: ignore[attr-defined]
            return async_guarded  # type: ignore[return-value]

        @functools.wraps(fn)
        def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
            self._require_role(role)
            return fn(self, *args, **kwargs)

        guarded.needs = role  # type: ignore[attr-defined]
        return guarded  # type: ignore[return-value]

    return decorate


# Public service methods that are not about a person's role: the serving path, maintenance,
# the machinery of signing in and of scoping, and what only the operator runs.
UNRESTRICTED = frozenset(
    {
        "for_scope", "owns", "find", "lookup", "open_pending", "record_candidates", "log_call",
        "prune_log", "prune_audit", "wait_for_answer", "sync_index", "sync_embeddings",
        "start_cluster", "stop_cluster", "create_workspace", "purge_workspaces",
        "authenticate_token", "sign_in_identity", "create_session", "authenticate_session",
        "end_session", "end_sessions_of", "workspaces_of", "membership_of", "principal_for_session",
        "switch_workspace", "invitation_preview", "accept_invitation",
    }
)  # fmt: skip
