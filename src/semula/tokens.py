"""API tokens for programs (ADR-0010): opaque, scoped, stored as a hash.

``smla_live_`` plus 32 random base62 characters (about 190 bits). The fixed prefix lets secret
scanners find a leaked token, and the same prefix is in the built-in redaction patterns, so a
token pasted into a prompt never reaches storage. Only the SHA-256 of a token is kept: the
secret is high-entropy, so a slow hash would add nothing. Pure functions; the service stores.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import string

PREFIX = "smla_live_"
ALPHABET = string.ascii_letters + string.digits
BODY_LENGTH = 32
SHAPE = re.compile(rf"{PREFIX}[A-Za-z0-9]{{{BODY_LENGTH}}}")

# The scopes, each including the ones below it (ADR-0010 section 2).
SCOPES = ("replay", "record", "admin")
LEVEL = {name: i for i, name in enumerate(SCOPES, start=1)}


def generate() -> str:
    return PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(BODY_LENGTH))


def looks_like_a_token(text: str) -> bool:
    return SHAPE.fullmatch(text) is not None


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def display_prefix(token: str) -> str:
    """What the token list shows to tell tokens apart: the fixed prefix and four characters."""
    return token[: len(PREFIX) + 4]


def coarse_address(address: str | None) -> str | None:
    """A client address cut to a network, so ``last used from`` is useful without being a log of
    people: IPv4 to its /24, IPv6 to its /48."""
    if not address:
        return None
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    prefix = 24 if ip.version == 4 else 48
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))
