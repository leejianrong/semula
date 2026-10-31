"""A limit per client address on the sign-in routes (SEM-115).

``/auth/*`` is open to anyone: it starts sign-ins, takes callbacks and invitation links, and each
request costs a database or provider call. One address gets a burst, then a steady rate. An IPv6
customer owns a whole /64, so the whole /64 is one address.
"""

from __future__ import annotations

import ipaddress
import math
import time
from collections.abc import Callable
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send


def address_key(address: str) -> str:
    """The thing to count: an IPv4 address, or the /64 an IPv6 address belongs to."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class AddressBuckets:
    """``per_minute`` requests at once, refilled steadily over the minute. One small entry per
    address seen recently; idle ones are dropped, and past ``max_addresses`` the oldest go."""

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_addresses: int = 10_000,
        prune_every: int = 256,
    ) -> None:
        self.burst = float(per_minute)
        self.rate = per_minute / 60.0
        self._clock = clock
        self._max = max_addresses
        self._every = prune_every
        self._seen = 0
        self._state: dict[str, tuple[float, float]] = {}

    def __len__(self) -> int:
        return len(self._state)

    def take(self, address: str) -> float:
        """0 if a request may go ahead, else the seconds until one may."""
        now = self._clock()
        key = address_key(address)
        level, stamp = self._state.get(key, (self.burst, now))
        level = min(self.burst, level + (now - stamp) * self.rate)
        self._seen += 1
        if self._seen % self._every == 0:
            self._prune(now)
        if level >= 1.0:
            self._state[key] = (level - 1.0, now)
            return 0.0
        self._state[key] = (level, now)
        return (1.0 - level) / self.rate

    def _prune(self, now: float) -> None:
        self._state = {
            k: v
            for k, v in self._state.items()
            if min(self.burst, v[0] + (now - v[1]) * self.rate) < self.burst
        }
        while len(self._state) > self._max:  # a flood of distinct addresses: forget the oldest
            self._state.pop(next(iter(self._state)))


class AuthRateLimit:
    """429 with ``Retry-After`` once an address has used its burst on ``/auth/*``."""

    def __init__(self, app: ASGIApp, per_minute: int) -> None:
        self.app = app
        self.buckets = AddressBuckets(per_minute)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not (
            scope["path"] == "/auth" or scope["path"].startswith("/auth/")
        ):
            await self.app(scope, receive, send)
            return
        client = (scope.get("client") or ("", 0))[0] or "unknown"
        wait = self.buckets.take(client)
        if wait:
            from .api_auth import _page  # the same page, headers included, as the other failures

            page: Any = _page(
                "Too many requests",
                "<p>Too many sign-in requests from your address. Try again in a minute.</p>",
                429,
            )
            page.headers["retry-after"] = str(math.ceil(wait))
            await page(scope, receive, send)
            return
        await self.app(scope, receive, send)
