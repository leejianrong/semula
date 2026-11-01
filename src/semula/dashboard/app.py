"""The indah application: sessions, theme and the live call topic."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import indah
from indah.hardening import Limits, Rate

from ..api_auth import Cookies
from ..config import Settings
from ..schema import DEFAULT_WORKSPACE
from ..scope import Principal, Scope
from ..service import Service
from .auth import POLICY, authenticator
from .shell import build_session, build_waiting_session
from .state import Dash
from .theme import build_theme

LOGO = Path(__file__).with_name("semula-mark.svg")


class Dashes:
    """One live-data hub per workspace, made when someone first opens that workspace, so every
    tab of a workspace shares its call topic and no tab hears another workspace's calls."""

    def __init__(self, service: Service) -> None:
        self._service = service
        self._by_workspace: dict[str, Dash] = {}
        self._lock = threading.Lock()

    def get(self, workspace_id: str) -> Dash:
        with self._lock:
            found = self._by_workspace.get(workspace_id)
            if found is None:
                found = Dash(self._service.for_scope(Scope(workspace_id)))
                self._by_workspace[workspace_id] = found
            return found

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            target = self._by_workspace.get(event.get("workspace", DEFAULT_WORKSPACE))
        if target is not None:
            target.publish(event)


def dashboard_limits(settings: Settings) -> Limits:
    """What one address may do to the dashboard (indah's ``hardening``). The rates are generous
    for a team behind one address and only stop a loop; the caps are the settings."""
    return Limits(
        rate={
            "/api/event": Rate(per_second=50.0, burst=200),
            "/api/upload": Rate(per_second=1.0, burst=10),
            "/api/stream": Rate(per_second=2.0, burst=30),
        },
        max_streams_per_ip=settings.ui_streams_per_address,
        max_sessions=settings.ui_sessions,
    )


def build_dashboard(service: Service, settings: Settings | None = None) -> Any:
    """The indah app. Locally it is open and serves the one workspace; when hosted every request
    is a signed-in person, every session belongs to one of them, and what a person sees and may
    do follows their role in the workspace they are working in (ADR-0010)."""
    hosted = settings is not None and settings.hosted
    dashes = Dashes(service)

    def build(ctx: indah.Context) -> indah.Session:
        if not hosted:
            return build_session(ctx, dashes.get(DEFAULT_WORKSPACE), service)
        user = ctx.user
        if user is None or user.workspace is None or not user.roles:
            return build_waiting_session(
                ctx,
                user,
                [w["workspace_id"] for w in service.suspended_workspaces_of(user.id)]
                if user is not None
                else [],
                settings.abuse_contact if settings else "",
            )
        role = next(iter(user.roles))
        workspace = user.workspace
        person = Principal(
            kind="person",
            id=user.id,
            workspace_id=workspace,
            role=role,
            live_role=lambda: service.membership_of(user.id, workspace),
        )
        acting = service.for_scope(Scope(user.workspace, person))
        return build_session(
            ctx,
            dashes.get(user.workspace),
            acting,
            who=user,
            public_url=settings.public_url if settings else "",
        )

    options: dict[str, Any] = {}
    if hosted and settings is not None:
        origin = Cookies(settings.public_url).origin
        options = {
            "auth": authenticator(service, Cookies(settings.public_url).session),
            "secure": True,
            "secret": settings.secret,
            "policy": POLICY,
            "allowed_origins": (origin,),
            "csp": "strict",
            "security_headers": True,
            "limits": dashboard_limits(settings),
        }
    app = indah.create_app(
        session_factory=build,
        title="semula",
        theme=build_theme(),
        spa=True,
        proxy_flush=False,  # a local container has no buffering proxy in front
        density="compact",
        logo=LOGO,
        **options,
    )
    app.state.dash = dashes
    app.state.publish = dashes.publish
    return app
