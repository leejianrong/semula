"""Signing people in with an external identity provider (ADR-0010 section 1).

semula is a relying party: no passwords, and no authorization server of its own. A provider is
OpenID Connect (Google, or any issuer you configure) or GitHub's plain OAuth 2.0, which has no
ID token and so is read from its API instead. The flow is the authorization-code flow with PKCE;
this module checks ``state``, ``nonce``, the issuer the response came from (RFC 9207), the
audience, the signature and expiry of the ID token, and accepts only a **verified** email.
A person is the pair (issuer, subject), never an email.

Pure protocol: it builds URLs, calls the provider and returns who signed in. Cookies, sessions
and routes are in ``api_auth``. PyJWT is imported only when a token is verified.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from .errors import SemulaError

FLOW_SECONDS = 600  # how long a sign-in may take between redirect and callback
DISCOVERY_SECONDS = 3600
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]  # never "none", never HMAC


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str  # oidc | github
    client_id: str
    client_secret: str
    issuer: str | None = None  # oidc: the issuer URL; github: https://github.com
    authorize_url: str | None = None  # github, or an oidc override
    token_url: str | None = None
    api_url: str = "https://api.github.com"  # github's API root
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    label: str = field(default="")

    @property
    def title(self) -> str:
        return self.label or self.name.capitalize()


@dataclass(frozen=True)
class Identity:
    """Who the provider says signed in. ``verified`` is true only if it vouches for the email."""

    issuer: str
    subject: str
    email: str
    verified: bool
    name: str = ""


def providers_from_env(env: dict[str, str]) -> dict[str, Provider]:
    """``SEMULA_AUTH_PROVIDERS=google,github,acme`` and, for each, ``SEMULA_AUTH_<NAME>_CLIENT_ID``
    and ``_CLIENT_SECRET`` (and ``_ISSUER`` for any name other than google and github)."""
    out: dict[str, Provider] = {}
    for raw in env.get("SEMULA_AUTH_PROVIDERS", "").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        key = f"SEMULA_AUTH_{name.upper()}_"
        client_id, secret = env.get(key + "CLIENT_ID", ""), env.get(key + "CLIENT_SECRET", "")
        if not client_id or not secret:
            raise ValueError(f"{key}CLIENT_ID and {key}CLIENT_SECRET are needed for '{name}'")
        if name == "github":
            out[name] = Provider(
                name, "github", client_id, secret, issuer="https://github.com",
                authorize_url=env.get(key + "AUTHORIZE_URL", "https://github.com/login/oauth/authorize"),
                token_url=env.get(key + "TOKEN_URL", "https://github.com/login/oauth/access_token"),
                api_url=env.get(key + "API_URL", "https://api.github.com"),
                scopes=("read:user", "user:email"), label="GitHub",
            )  # fmt: skip
        else:
            issuer = env.get(key + "ISSUER") or (
                "https://accounts.google.com" if name == "google" else ""
            )
            if not issuer:
                raise ValueError(f"{key}ISSUER is needed for '{name}'")
            out[name] = Provider(name, "oidc", client_id, secret, issuer=issuer.rstrip("/"))
    return out


# --- the flow cookie: state, nonce and PKCE verifier, signed so the browser cannot change them ----
def seal(secret: bytes, data: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).rstrip(b"=")
    mac = hmac.new(secret, body, hashlib.sha256).digest()
    return body.decode() + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def unseal(secret: bytes, token: str) -> dict[str, Any] | None:
    try:
        body, mac = token.split(".", 1)
        expected = base64.urlsafe_b64encode(
            hmac.new(secret, body.encode(), hashlib.sha256).digest()
        ).rstrip(b"=")
        if not hmac.compare_digest(expected, mac.encode()):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, KeyError):
        return None
    return data if isinstance(data, dict) and data.get("exp", 0) > time.time() else None


def new_flow(provider: Provider, redirect_uri: str, next_path: str) -> tuple[dict[str, Any], str]:
    """A fresh sign-in attempt: the data to seal into the flow cookie, and the PKCE challenge."""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    flow = {
        "provider": provider.name,
        "state": secrets.token_urlsafe(24),
        "nonce": secrets.token_urlsafe(24),
        "verifier": verifier,
        "redirect_uri": redirect_uri,
        "next": next_path,
        "exp": time.time() + FLOW_SECONDS,
    }
    return flow, challenge


class Client:
    """Talks to providers. ``transport`` is a test seam (an ASGI app standing in for the IdP)."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._discovery: dict[str, tuple[float, dict[str, Any]]] = {}
        self._jwks: dict[str, Any] = {}

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=10.0, follow_redirects=False)

    async def _discover(self, provider: Provider) -> dict[str, Any]:
        cached = self._discovery.get(provider.name)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if not provider.issuer:
            raise SemulaError(500, "provider_misconfigured", f"{provider.name} has no issuer.")
        async with self._http() as http:
            resp = await http.get(provider.issuer + "/.well-known/openid-configuration")
        resp.raise_for_status()
        doc: dict[str, Any] = resp.json()
        if str(doc.get("issuer", "")).rstrip("/") != provider.issuer.rstrip("/"):
            raise SemulaError(
                502, "provider_mismatch", "The provider's issuer is not the configured one."
            )
        self._discovery[provider.name] = (time.monotonic() + DISCOVERY_SECONDS, doc)
        return doc

    async def authorize_url(self, provider: Provider, flow: dict[str, Any], challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": flow["redirect_uri"],
            "scope": " ".join(provider.scopes),
            "state": flow["state"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if provider.kind == "oidc":
            doc = await self._discover(provider)
            params["nonce"] = flow["nonce"]
            base = provider.authorize_url or doc["authorization_endpoint"]
        else:
            base = str(provider.authorize_url)
        return f"{base}?{urlencode(params)}"

    async def identify(
        self, provider: Provider, flow: dict[str, Any], query: dict[str, str]
    ) -> Identity:
        """Finish a sign-in from the callback's query string. Raises ``SemulaError`` (400 or 502)
        with a stable code for every way it can go wrong."""
        if query.get("error"):
            raise SemulaError(400, "sign_in_refused", f"The provider refused: {query['error']}.")
        if not hmac.compare_digest(str(query.get("state", "")), str(flow["state"])):
            raise SemulaError(400, "invalid_state", "The sign-in did not start here. Try again.")
        code = query.get("code")
        if not code:
            raise SemulaError(400, "invalid_request", "The provider sent no code.")
        if provider.kind == "github":
            return await self._github(provider, flow, code)
        doc = await self._discover(provider)
        sent = query.get("iss")  # RFC 9207: the issuer the response came from
        if sent is not None and sent.rstrip("/") != provider.issuer.rstrip("/"):  # type: ignore[union-attr]
            raise SemulaError(400, "issuer_mismatch", "The response came from another provider.")
        token = await self._exchange(
            provider, flow, code, provider.token_url or doc["token_endpoint"]
        )
        claims = await self._verify_id_token(provider, doc, flow, token)
        return Identity(
            issuer=provider.issuer.rstrip("/"),  # type: ignore[union-attr]
            subject=str(claims["sub"]),
            email=str(claims.get("email", "")),
            verified=claims.get("email_verified") is True,
            name=str(claims.get("name", "")),
        )

    async def _exchange(
        self, provider: Provider, flow: dict[str, Any], code: str, url: str
    ) -> dict[str, Any]:
        async with self._http() as http:
            resp = await http.post(
                url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": flow["redirect_uri"],
                    "client_id": provider.client_id,
                    "client_secret": provider.client_secret,
                    "code_verifier": flow["verifier"],
                },
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            raise SemulaError(502, "token_exchange_failed", "The provider would not issue a token.")
        body: dict[str, Any] = resp.json()
        return body

    async def _verify_id_token(
        self, provider: Provider, doc: dict[str, Any], flow: dict[str, Any], token: dict[str, Any]
    ) -> dict[str, Any]:
        import jwt

        raw = token.get("id_token")
        if not raw:
            raise SemulaError(502, "no_id_token", "The provider sent no ID token.")
        try:
            header = jwt.get_unverified_header(raw)
            if header.get("alg") not in ALGORITHMS:
                raise SemulaError(
                    400,
                    "invalid_id_token",
                    "The ID token is not signed with a supported algorithm.",
                )
            signing = await self._signing_key(doc["jwks_uri"], header.get("kid"))
            claims: dict[str, Any] = jwt.decode(
                raw,
                signing,
                algorithms=ALGORITHMS,
                audience=provider.client_id,
                issuer=provider.issuer.rstrip("/"),  # type: ignore[union-attr]
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                leeway=30,
            )
        except SemulaError:
            raise
        except jwt.PyJWTError as exc:
            raise SemulaError(
                400, "invalid_id_token", f"The ID token is not valid: {exc}."
            ) from exc
        if not hmac.compare_digest(str(claims.get("nonce", "")), str(flow["nonce"])):
            raise SemulaError(400, "invalid_nonce", "The ID token was not issued for this sign-in.")
        return claims

    async def _signing_key(self, uri: str, kid: str | None) -> Any:
        """The provider's key for ``kid``. Fetched through our own HTTP client (so the same
        timeouts and test seam apply) and refetched once if the key is not in the cached set,
        which is how a provider rotates keys."""
        import jwt

        for attempt in (0, 1):
            cached = self._jwks.get(uri)
            if cached is None or attempt:
                async with self._http() as http:
                    resp = await http.get(uri)
                resp.raise_for_status()
                cached = jwt.PyJWKSet.from_dict(resp.json())
                self._jwks[uri] = cached
            for key in cached.keys:
                if key.key_id == kid:
                    return key.key
        raise SemulaError(400, "invalid_id_token", "The ID token is signed with an unknown key.")

    async def _github(self, provider: Provider, flow: dict[str, Any], code: str) -> Identity:
        token = await self._exchange(provider, flow, code, str(provider.token_url))
        access = token.get("access_token")
        if not access:
            raise SemulaError(502, "token_exchange_failed", "GitHub would not issue a token.")
        headers = {"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"}
        async with self._http() as http:
            user = await http.get(f"{provider.api_url}/user", headers=headers)
            emails = await http.get(f"{provider.api_url}/user/emails", headers=headers)
        if user.status_code != 200 or emails.status_code != 200:
            raise SemulaError(502, "profile_failed", "GitHub would not give the profile.")
        verified = [e for e in emails.json() if e.get("verified") and e.get("primary")]
        profile = user.json()
        return Identity(
            issuer="https://github.com",
            subject=str(profile["id"]),
            email=str(verified[0]["email"]) if verified else "",
            verified=bool(verified),
            name=str(profile.get("name") or profile.get("login") or ""),
        )
