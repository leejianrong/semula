"""A stand-in identity provider for sign-in tests: OpenID Connect (discovery, keys, authorize,
token) and the GitHub-style OAuth 2.0 endpoints, in one small ASGI app.

``Provider.next`` is who signs in next; ``tamper`` breaks one thing in the response so a test can
watch semula refuse it. It is the real protocol with a fake person, which is what the tests need:
semula talks to it exactly as it would to Google, through an ASGI transport.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

ISSUER = "https://idp.test"
CLIENT_ID = "semula-client"
CLIENT_SECRET = "s3cret-for-tests"  # noqa: S105


def _b64(number: int) -> str:
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Provider:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.next: dict[str, Any] = {
            "sub": "idp-user-1",
            "email": "ada@example.com",
            "email_verified": True,
            "name": "Ada Lovelace",
        }
        self.tamper: str | None = None
        self.codes: dict[str, dict[str, Any]] = {}
        self.github_user: dict[str, Any] = {"id": 4242, "login": "ada", "name": "Ada"}
        self.github_emails: list[dict[str, Any]] = [
            {"email": "ada@example.com", "primary": True, "verified": True}
        ]
        self.app = Starlette(
            routes=[
                Route("/.well-known/openid-configuration", self.discovery),
                Route("/jwks", self.jwks),
                Route("/authorize", self.authorize),
                Route("/token", self.token, methods=["POST"]),
                Route("/github/authorize", self.authorize),
                Route("/github/token", self.github_token, methods=["POST"]),
                Route("/user", self.user),
                Route("/user/emails", self.emails),
            ]
        )

    # --- OpenID Connect ----------------------------------------------------------------
    async def discovery(self, request: Request) -> Response:
        issuer = ISSUER if self.tamper != "discovery_issuer" else "https://evil.test"
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks",
            }
        )

    async def jwks(self, request: Request) -> Response:
        numbers = self.key.public_key().public_numbers()
        return JSONResponse(
            {
                "keys": [
                    {
                        "kty": "RSA",
                        "kid": "k1",
                        "alg": "RS256",
                        "use": "sig",
                        "n": _b64(numbers.n),
                        "e": _b64(numbers.e),
                    }  # fmt: skip
                ]
            }
        )

    async def authorize(self, request: Request) -> Response:
        q = dict(request.query_params)
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "client_id": q["client_id"],
            "redirect_uri": q["redirect_uri"],
            "nonce": q.get("nonce"),
            "challenge": q.get("code_challenge"),
            "method": q.get("code_challenge_method"),
            "user": dict(self.next),
        }
        state = q["state"] if self.tamper != "state" else "someone-elses-state"
        params = {"code": code, "state": state}
        if self.tamper == "iss":
            params["iss"] = "https://evil.test"
        elif q.get("nonce") is not None:
            params["iss"] = ISSUER
        if self.tamper == "denied":
            params = {"error": "access_denied", "state": state}
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode(params)}", status_code=302)

    def _id_token(self, record: dict[str, Any]) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": record["nonce"],
            **record["user"],
        }
        key, alg = self.key, "RS256"
        match self.tamper:
            case "nonce":
                claims["nonce"] = "another-nonce"
            case "aud":
                claims["aud"] = "someone-elses-client"
            case "iss_claim":
                claims["iss"] = "https://evil.test"
            case "expired":
                claims["exp"] = now - 3600
            case "signature":
                key = self.other_key  # signed by a key the provider never published
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        if self.tamper == "alg_none":
            return jwt.encode(claims, None, algorithm="none", headers={"kid": "k1"})
        return jwt.encode(claims, pem, algorithm=alg, headers={"kid": "k1"})

    async def token(self, request: Request) -> Response:
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        record = self.codes.pop(form.get("code", ""), None)  # a code works once
        if (
            record is None
            or form.get("client_id") != CLIENT_ID
            or form.get("client_secret") != CLIENT_SECRET
            or form.get("redirect_uri") != record["redirect_uri"]
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        wanted = (
            base64.urlsafe_b64encode(
                hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        if record["method"] != "S256" or wanted != record["challenge"]:
            return JSONResponse({"error": "invalid_grant", "detail": "pkce"}, status_code=400)
        return JSONResponse(
            {"access_token": "at", "token_type": "Bearer", "id_token": self._id_token(record)}
        )

    # --- GitHub-style OAuth 2.0 ------------------------------------------------------------
    async def github_token(self, request: Request) -> Response:
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        record = self.codes.pop(form.get("code", ""), None)
        if record is None or form.get("client_secret") != CLIENT_SECRET:
            return JSONResponse({"error": "bad_verification_code"}, status_code=200)
        return JSONResponse({"access_token": "gh-token", "token_type": "bearer"})

    async def user(self, request: Request) -> Response:
        if request.headers.get("authorization") != "Bearer gh-token":
            return JSONResponse({"message": "Bad credentials"}, status_code=401)
        return JSONResponse(self.github_user)

    async def emails(self, request: Request) -> Response:
        if request.headers.get("authorization") != "Bearer gh-token":
            return JSONResponse({"message": "Bad credentials"}, status_code=401)
        return JSONResponse(self.github_emails)
