"""Sign-in routes for people (ADR-0010 section 1): ``/auth``.

Login, the provider's callback, sign-out and "who am I". The dashboard is what a signed-in
person uses; the REST API and MCP stay token-only. The session cookie is opaque, ``HttpOnly``,
``SameSite=Lax`` and, on https, ``Secure`` with the ``__Host-`` prefix. A new session is made at
every sign-in and the one it replaces is ended. Signing out needs the session's CSRF token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool as offload

from . import oidc
from . import tokens as tok
from .errors import SemulaError
from .service import DEVICE_SCOPE_FOR, SESSION_SECONDS, Service

log = logging.getLogger("semula.auth")
router = APIRouter(prefix="/auth")
LANDING = "/ui/"


class Cookies:
    """The cookie names and flags for this server's address (a ``__Host-`` name needs https)."""

    def __init__(self, public_url: str) -> None:
        self.secure = public_url.startswith("https://")
        prefix = "__Host-" if self.secure else ""
        self.session = f"{prefix}semula_session"
        self.flow = f"{prefix}semula_flow"
        self.origin = f"{urlparse(public_url).scheme}://{urlparse(public_url).netloc}"

    def set(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path="/",
            httponly=True,
            secure=self.secure,
            samesite="lax",
        )

    def clear(self, response: Response, name: str) -> None:
        response.delete_cookie(name, path="/", secure=self.secure, httponly=True, samesite="lax")


def _state(request: Request) -> tuple[Service, oidc.Client, Cookies, bytes]:
    app = request.app
    settings = app.state.settings
    return app.state.service, app.state.oidc, app.state.cookies, settings.secret.encode()


def safe_next(target: str | None) -> str:
    """Only a path on this site: no scheme, no host, no protocol-relative ``//``."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return LANDING
    parsed = urlparse(target)
    return target if not parsed.scheme and not parsed.netloc else LANDING


_STYLE = (
    "body{font:16px system-ui;max-width:26rem;margin:4rem auto;padding:0 1rem}"
    "a.b{display:block;margin:.6rem 0;padding:.7rem 1rem;border:1px solid #888;"
    "border-radius:8px;"
    "text-decoration:none;color:inherit}"
)
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()
# These pages run no script and cannot be framed; their one style block is allowed by its hash,
# and the only thing they submit goes back to this site.
_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; "
        f"style-src 'sha256-{_STYLE_HASH}'; img-src data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    page = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{html.escape(title)}</title><link rel=icon href='data:,'><style>{_STYLE}</style>"
        f"<h1>{html.escape(title)}</h1>{body}"
    )
    return HTMLResponse(page, status_code=status, headers=_PAGE_HEADERS)


@router.get("/providers")
def providers(request: Request) -> dict[str, Any]:
    return {
        "providers": [
            {"name": p.name, "title": p.title}
            for p in request.app.state.settings.auth_providers.values()
        ]
    }


@router.get("/login", response_model=None)
def login_page(request: Request, next: str | None = None) -> HTMLResponse:
    found = request.app.state.settings.auth_providers
    target = safe_next(next)
    buttons = "".join(
        f'<a class=b href="/auth/login/{html.escape(p.name)}?{_query({"next": target})}">'
        f"Continue with {html.escape(p.title)}</a>"
        for p in found.values()
    )
    return _page("Sign in to semula", buttons or "<p>No sign-in provider is configured.</p>")


def _query(params: dict[str, str]) -> str:
    from urllib.parse import urlencode

    return urlencode(params)


# What a provider that is down, slow or answering rubbish raises inside ``oidc.Client``.
PROVIDER_FAILURES = (httpx.HTTPError, json.JSONDecodeError)


def _sign_in_failed(exc: Exception, provider: str) -> HTMLResponse:
    """The page for a sign-in that did not work. A provider that cannot be reached says so
    plainly and logs the kind of failure, never the address, which may hold a secret."""
    if isinstance(exc, SemulaError):
        err = exc
    else:
        log.warning("sign-in provider %s failed: %s", provider, type(exc).__name__)
        err = SemulaError(
            502,
            "provider_unreachable",
            "The sign-in provider could not be reached. Try again in a moment.",
        )
    return _page(
        "Sign-in failed",
        f"<p>{html.escape(err.message)}</p><p><code>{html.escape(err.code)}</code></p>"
        '<p><a href="/auth/login">Try again</a></p>',
        err.status if err.status < 500 else 502,
    )


@router.get("/login/{provider}", response_model=None)
async def start(provider: str, request: Request, next: str | None = None) -> Response:
    service, client, cookies, secret = _state(request)
    chosen = request.app.state.settings.auth_providers.get(provider)
    if chosen is None:
        raise SemulaError(404, "provider_not_found", f"No sign-in provider '{provider}'.")
    redirect_uri = f"{cookies.origin}/auth/callback/{provider}"
    flow, challenge = oidc.new_flow(chosen, redirect_uri, safe_next(next))
    try:
        where = await client.authorize_url(chosen, flow, challenge)
    except (SemulaError, *PROVIDER_FAILURES) as exc:
        return _sign_in_failed(exc, provider)
    response = RedirectResponse(where, status_code=302)
    cookies.set(response, cookies.flow, oidc.seal(secret, flow), oidc.FLOW_SECONDS)
    return response


@router.get("/callback/{provider}", response_model=None)
async def callback(provider: str, request: Request) -> Response:
    service, client, cookies, secret = _state(request)
    chosen = request.app.state.settings.auth_providers.get(provider)
    flow = oidc.unseal(secret, request.cookies.get(cookies.flow, ""))
    try:
        if chosen is None or flow is None or flow.get("provider") != provider:
            raise SemulaError(400, "invalid_state", "The sign-in did not start here. Try again.")
        who = await client.identify(chosen, flow, dict(request.query_params))
        person = await offload(service.sign_in_identity, who)
        secret_value, _ = await offload(
            lambda: service.create_session(
                person["id"],
                client=request.client.host if request.client else None,
                replacing=request.cookies.get(cookies.session),
            )
        )
    except (SemulaError, *PROVIDER_FAILURES) as exc:
        failed = _sign_in_failed(exc, provider)
        cookies.clear(failed, cookies.flow)
        return failed
    response = RedirectResponse(flow["next"], status_code=302)
    cookies.clear(response, cookies.flow)
    cookies.set(response, cookies.session, secret_value, SESSION_SECONDS)
    return response


@router.get("/me")
async def me(request: Request) -> JSONResponse:
    service, _, cookies, _ = _state(request)
    session = await offload(service.authenticate_session, request.cookies.get(cookies.session, ""))
    if session is None:
        return JSONResponse(
            SemulaError(401, "not_signed_in", "You are not signed in.").body(), status_code=401
        )
    return JSONResponse(
        {
            "person": {
                "id": session["person_id"],
                "email": session["email"],
                "name": session["name"],
            },
            "csrf": session["csrf"],
        }
    )


@router.post("/logout", response_model=None)
async def logout(request: Request) -> Response:
    service, _, cookies, _ = _state(request)
    secret = request.cookies.get(cookies.session, "")
    session = await offload(service.authenticate_session, secret)
    origin = request.headers.get("origin")
    form = await request.form()
    sent = request.headers.get("x-csrf-token") or str(form.get("csrf", ""))
    if (
        session is None
        or (origin is not None and origin != cookies.origin)
        or not hmac.compare_digest(sent, session["csrf"])
    ):
        raise SemulaError(403, "csrf", "Sign-out needs the session's CSRF token.")
    await offload(service.end_session, secret)
    response = RedirectResponse("/auth/login", status_code=303)
    cookies.clear(response, cookies.session)
    return response


def _invite_page(preview: dict[str, Any], token: str, csrf: str, email: str) -> HTMLResponse:
    body = (
        f"<p>You are signed in as <b>{html.escape(email)}</b>.</p>"
        f"<p>Join <b>{html.escape(preview['workspace'])}</b> as "
        f"<b>{html.escape(preview['role'])}</b>?</p>"
        f'<form method=post action="/auth/invite/{html.escape(token)}">'
        f'<input type=hidden name=csrf value="{html.escape(csrf)}">'
        "<button>Join</button></form>"
    )
    return _page("Join a workspace", body)


@router.get("/invite/{token}", response_model=None)
async def invitation(token: str, request: Request) -> Response:
    """The link in an invitation. Sign in first (any provider), then confirm."""
    service, _, cookies, _ = _state(request)
    session = await offload(service.authenticate_session, request.cookies.get(cookies.session, ""))
    if session is None:
        return RedirectResponse(
            f"/auth/login?{_query({'next': f'/auth/invite/{token}'})}", status_code=302
        )
    try:
        preview = await offload(service.invitation_preview, token)
    except SemulaError as exc:
        return _page("Invitation", f"<p>{html.escape(exc.message)}</p>", exc.status)
    return _invite_page(preview, token, session["csrf"], session["email"])


def _signup_page(csrf: str, email: str) -> HTMLResponse:
    body = (
        f"<p>You are signed in as <b>{html.escape(email)}</b>.</p>"
        "<p>Enter the invite code you were sent to get a workspace of your own.</p>"
        '<form method=post action="/auth/signup">'
        f'<input type=hidden name=csrf value="{html.escape(csrf)}">'
        "<p><input name=code autocomplete=off autofocus required size=36 "
        'placeholder="smla_code_..."></p>'
        "<button>Create my workspace</button></form>"
    )
    return _page("Create your workspace", body)


@router.get("/signup", response_model=None)
async def signup_page(request: Request) -> Response:
    """Sign in first (any provider), then enter an invite code."""
    service, _, cookies, _ = _state(request)
    session = await offload(service.authenticate_session, request.cookies.get(cookies.session, ""))
    if session is None:
        return RedirectResponse(f"/auth/login?{_query({'next': '/auth/signup'})}", status_code=302)
    return _signup_page(session["csrf"], session["email"])


@router.post("/signup", response_model=None)
async def signup(request: Request) -> Response:
    service, _, cookies, _ = _state(request)
    session = await offload(service.authenticate_session, request.cookies.get(cookies.session, ""))
    form = await request.form()
    origin = request.headers.get("origin")
    if (
        session is None
        or (origin is not None and origin != cookies.origin)
        or not hmac.compare_digest(str(form.get("csrf", "")), session["csrf"])
    ):
        raise SemulaError(403, "csrf", "Signing up needs the session's CSRF token.")
    try:
        made = await offload(
            service.redeem_signup_code, str(form.get("code", "")), session["person_id"]
        )
    except SemulaError as exc:
        return _page(
            "Create your workspace",
            f'<p>{html.escape(exc.message)}</p><p><a href="/auth/signup">Try again</a></p>',
            exc.status,
        )
    await offload(service.switch_session, session["session_id"], made["workspace_id"])
    return RedirectResponse(LANDING, status_code=303)


# --- signing the CLI in (SEM-118) ----------------------------------------------------------


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise SemulaError(400, "invalid_json", "The request body is not valid JSON.") from exc
    if not isinstance(body, dict):
        raise SemulaError(400, "invalid_request", "The body must be a JSON object.")
    return body


def _text(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is not None and not isinstance(value, str):
        raise SemulaError(400, "invalid_request", f"'{key}' must be text.", param=key)
    return value


@router.post("/device/code")
async def device_code(request: Request) -> JSONResponse:
    """The CLI asks to be signed in. Body: ``{"scope"?, "project"?, "name"?}``. Nothing is granted
    until a signed-in person approves the short code in a browser."""
    service, _, cookies, _ = _state(request)
    body = await _body(request)
    started = await offload(
        service.device_start,
        _text(body, "scope") or "record",
        _text(body, "project"),
        _text(body, "name") or "semula CLI",
    )
    base = request.app.state.settings.public_url
    return JSONResponse(
        {
            **started,
            "verification_url": f"{base}/auth/device?{_query({'code': started['user_code']})}",
        }
    )


@router.post("/device/token")
async def device_token(request: Request) -> JSONResponse:
    """The CLI polls with its secret code: ``pending``, ``denied``, or (once) its token."""
    service, _, _, _ = _state(request)
    body = await _body(request)
    got = await offload(service.device_poll, _text(body, "device_code") or "")
    return JSONResponse(got)


def _device_page(preview: dict[str, Any], role: str, workspace: str, csrf: str) -> HTMLResponse:
    granted = min(preview["scope"], DEVICE_SCOPE_FOR[role], key=lambda s: tok.LEVEL[s])
    where = html.escape(preview["project"]) if preview["project"] else "all projects"
    body = (
        "<p>A program asked to sign in to semula. Approve it only if you just ran "
        "<code>semula login</code> and this code matches your terminal.</p>"
        f"<h2>{html.escape(preview['user_code'])}</h2>"
        f"<p>Name: <b>{html.escape(preview['name'])}</b><br>"
        f"Workspace: <b>{html.escape(workspace)}</b><br>"
        f"Access: <b>{html.escape(granted)}</b> to <b>{where}</b></p>"
        '<form method=post action="/auth/device">'
        f'<input type=hidden name=csrf value="{html.escape(csrf)}">'
        f'<input type=hidden name=code value="{html.escape(preview["user_code"])}">'
        "<button name=decision value=approve>Approve</button> "
        "<button name=decision value=deny>Deny</button></form>"
    )
    return _page("Sign in the CLI", body)


@router.get("/device", response_model=None)
async def device_page(request: Request, code: str = "") -> Response:
    service, _, cookies, _ = _state(request)
    session = await offload(service.authenticate_session, request.cookies.get(cookies.session, ""))
    if session is None:
        target = f"/auth/device?{_query({'code': code})}"
        return RedirectResponse(f"/auth/login?{_query({'next': target})}", status_code=302)
    try:
        preview = await offload(service.device_preview, code)
    except SemulaError as exc:
        return _page("Sign in the CLI", f"<p>{html.escape(exc.message)}</p>", exc.status)
    acting = await offload(service.principal_for_session, session)
    if acting is None or acting.role is None:
        return _page(
            "Sign in the CLI",
            "<p>You do not belong to a workspace yet, so there is nothing to sign in to. "
            'Join one first (an invitation, or <a href="/auth/signup">an invite code</a>).</p>',
        )
    return _device_page(preview, acting.role, acting.workspace_id, session["csrf"])


@router.post("/device", response_model=None)
async def device_decision(request: Request) -> Response:
    service, _, cookies, _ = _state(request)
    session = await offload(service.authenticate_session, request.cookies.get(cookies.session, ""))
    form = await request.form()
    origin = request.headers.get("origin")
    if (
        session is None
        or (origin is not None and origin != cookies.origin)
        or not hmac.compare_digest(str(form.get("csrf", "")), session["csrf"])
    ):
        raise SemulaError(403, "csrf", "Signing in the CLI needs the session's CSRF token.")
    approve = form.get("decision") == "approve"
    acting = await offload(service.principal_for_session, session)
    try:
        await offload(
            service.device_decide,
            str(form.get("code", "")),
            session["person_id"],
            acting.workspace_id if acting else "",
            approve,
        )
    except SemulaError as exc:
        return _page("Sign in the CLI", f"<p>{html.escape(exc.message)}</p>", exc.status)
    return _page(
        "Sign in the CLI",
        "<p>Approved. You can close this tab; the terminal will finish signing in.</p>"
        if approve
        else "<p>Denied. Nothing was signed in.</p>",
    )


@router.post("/invite/{token}", response_model=None)
async def accept(token: str, request: Request) -> Response:
    service, _, cookies, _ = _state(request)
    session = await offload(service.authenticate_session, request.cookies.get(cookies.session, ""))
    form = await request.form()
    origin = request.headers.get("origin")
    if (
        session is None
        or (origin is not None and origin != cookies.origin)
        or not hmac.compare_digest(str(form.get("csrf", "")), session["csrf"])
    ):
        raise SemulaError(403, "csrf", "Joining needs the session's CSRF token.")
    try:
        await offload(service.accept_invitation, token, session["person_id"])
    except SemulaError as exc:
        return _page("Invitation", f"<p>{html.escape(exc.message)}</p>", exc.status)
    return RedirectResponse(LANDING, status_code=303)
