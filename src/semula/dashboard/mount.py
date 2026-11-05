"""Build the dashboard after start-up and serve it at ``/ui``."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import threading
from typing import Any
from urllib.parse import urlencode

from starlette.types import Receive, Scope, Send

from ..api_auth import Cookies
from ..config import Settings
from ..public_page import page as public_page
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


async def _redirect(send: Send, location: str, status: int = 307) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"location", location.encode()), (b"content-length", b"0")],
        }
    )
    await send({"type": "http.response.body", "body": b""})


def waiting_page(
    email: str, csrf: str, suspended: list[str], abuse_contact: str, signup_open: bool
) -> Any:
    """What a signed-in person sees who cannot use any workspace: the site's own page (as sign-in
    and the terms), not the dashboard. It tells them why, and Sign out is the way out, for
    signing in as someone else. Every value is escaped; nothing here runs script."""
    way_out = (
        '<form method=post action="/auth/logout">'
        f'<input type=hidden name=csrf value="{html.escape(csrf)}">'
        "<button class=alt>Sign out</button></form>"
    )
    if suspended:
        names = html.escape(", ".join(suspended))
        where = (
            f"Write to {html.escape(abuse_contact)}." if abuse_contact else "Contact the operator."
        )
        body = (
            f"<p>The workspace {names} is suspended, so nothing in it can be used for now. "
            f"Its data is kept. {where}</p><p><small>The <a href=/terms>terms</a>.</small></p>"
        )
        return public_page("Workspace suspended", body + way_out)
    more = (
        "<a href=/auth/signup>Create a workspace of your own</a>, or open the invitation link "
        "you were sent."
        if signup_open
        else "Or, if you were sent an invite code, <a href=/auth/signup>enter it here</a>."
    )
    body = (
        f"<p>You are signed in as <b>{html.escape(email)}</b>, but you do not belong to a "
        "workspace. Ask an admin to invite you, and open the link they send.</p>"
        f"<p>{more}</p>"
    )
    return public_page("No workspace yet", body + way_out)


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

    def __init__(self, service: Service, settings: Settings | None = None) -> None:
        self._service = service
        self._settings = settings
        self._hosted = settings is not None and settings.hosted
        self._cookie = Cookies(settings.public_url).session if settings else ""
        self._app: Any = None
        self._bridge_task: asyncio.Task[None] | None = None
        self._built = asyncio.Event()
        self._build_lock = threading.Lock()

    def _build(self) -> Any:
        with self._build_lock:
            if self._app is None:
                from .app import build_dashboard

                self._app = build_dashboard(self._service, self._settings)
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

    async def _session(self, scope: Scope) -> dict[str, Any] | None:
        """The live sign-in this page request carries, if any. (The app's own ``/api`` calls
        answer 401 themselves; a page load is sent to the sign-in page instead.)"""
        cookies = {
            k.strip(): v
            for part in dict(scope["headers"]).get(b"cookie", b"").decode().split(";")
            if "=" in part
            for k, v in [part.split("=", 1)]
        }
        return await asyncio.to_thread(
            self._service.authenticate_session, cookies.get(self._cookie, "")
        )

    async def _waiting(self, session: dict[str, Any]) -> Any:
        """The page for a person who belongs to no usable workspace, else None."""
        found = await asyncio.to_thread(self._service.principal_for_session, session)
        if found is not None and found.role and found.workspace_id:
            return None
        suspended = await asyncio.to_thread(
            self._service.suspended_workspaces_of, session["person_id"]
        )
        settings = self._settings
        return waiting_page(
            session["email"],
            session["csrf"],
            [w["workspace_id"] for w in suspended],
            settings.abuse_contact if settings else "",
            settings is not None and settings.signup == "open",
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self._hosted
            and scope["type"] == "http"
            and not scope["path"].startswith(f"{scope.get('root_path', '')}/api/")
        ):
            session = await self._session(scope)
            if session is None:
                await _redirect(send, "/auth/login?" + urlencode({"next": scope["path"]}), 302)
                return
            waiting = await self._waiting(session)
            if waiting is not None:
                await waiting(scope, receive, send)
                return
        if scope["type"] == "http" and scope["path"].rstrip("/") == scope.get("root_path", ""):
            project = (
                DEFAULT_PROJECT if self._hosted else await asyncio.to_thread(self._landing_project)
            )
            await _redirect(send, f"{scope.get('root_path', '')}/projects/{project}/log")
            return
        app = await self._ensure()
        if app is None:
            if scope["type"] == "http":
                await _plain(send, 503, "The dashboard is not ready.")
            return
        await app(scope, receive, send)
