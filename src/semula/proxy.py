"""The client's address behind a reverse proxy (SEM-111).

Behind Caddy the socket peer is always the proxy, so the failed-token limiter, a token's "last
used from" and the audit log would all see one address. The proxy writes the real one into
``X-Forwarded-For``, but anyone can send that header, so it is believed only when the peer is a
proxy the operator named, and read from the right: each trusted proxy appends the address it
received the request from, so the first address from the right that is not a trusted proxy is the
client. Anything further left was written by the client and is never used.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(items: Iterable[str]) -> list[Network]:
    return [ipaddress.ip_network(item, strict=False) for item in items]


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        return None


def _trusted(text: str, networks: list[Network]) -> bool:
    addr = _address(text)
    return addr is not None and any(addr in net for net in networks)


def client_address(peer: str, forwarded: str, networks: list[Network]) -> str:
    """The address to treat as the client for a request from ``peer`` carrying ``forwarded``."""
    if not networks or not _trusted(peer, networks) or not forwarded:
        return peer
    for hop in reversed([h.strip() for h in forwarded.split(",") if h.strip()]):
        if _address(hop) is None:
            return peer  # a hop we cannot read: believe none of it
        if not _trusted(hop, networks):
            return hop
    return peer  # every hop was one of our own proxies


class ForwardedClient:
    """Puts the real client address in the ASGI scope, where every layer already reads it."""

    def __init__(self, app: Any, trusted: Iterable[str]) -> None:
        self.app = app
        self.networks = parse_networks(trusted)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket") and self.networks and scope.get("client"):
            peer, port = scope["client"]
            header = b",".join(v for k, v in scope["headers"] if k == b"x-forwarded-for")
            real = client_address(peer, header.decode("latin-1"), self.networks)
            if real != peer:
                scope["client"] = (real, port)
        await self.app(scope, receive, send)
