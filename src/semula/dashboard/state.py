"""What every dashboard tab shares: the service, the live topics, a few helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import indah

from ..service import Service
from .rows import call_row

T = TypeVar("T")
SEED_CALLS = 300  # recent calls a freshly built dashboard already knows about


class Dash:
    """The service plus two topics: ``calls`` (each log row, shaped for the table) and
    ``events`` (everything else the service announces: queues, mode, projects)."""

    def __init__(self, service: Service) -> None:
        self.service = service
        self.calls: indah.Topic[dict[str, Any]] = indah.Topic("calls", replay=1000)
        self.events: indah.Topic[dict[str, Any]] = indah.Topic("events", replay=0)
        seeded = list(reversed(service.list_calls(limit=SEED_CALLS)))  # oldest first
        self._seeded_up_to = max((c["id"] for c in seeded), default=0)
        for call in seeded:
            self.calls.publish(call_row(call))

    def publish(self, event: dict[str, Any]) -> None:
        """Called on the event loop for every service event."""
        if event.get("type") == "call":
            if event["id"] > self._seeded_up_to:  # else the seed already has it
                self.calls.publish(call_row(event))
        else:
            self.events.publish(event)

    @property
    def meaning_on(self) -> bool:
        return self.service.vectors is not None

    def project_ids(self) -> list[str]:
        return [p["id"] for p in self.service.list_projects()]


async def blocking(fn: Callable[..., T], *args: Any) -> T:
    """Run a service call off the event loop: indah runs handlers on it, and a database call
    there would stall every open tab."""
    return await asyncio.to_thread(fn, *args)


def every(ctx: indah.Context, seconds: float, fn: Callable[[], Awaitable[None]]) -> None:
    """Run ``fn`` now and then every ``seconds`` for as long as the tab is open."""

    async def loop() -> None:
        while True:
            try:
                await fn()
            except Exception:  # a failed refresh must not end the loop
                import logging

                logging.getLogger("semula.dashboard").exception("refresh failed")
            await asyncio.sleep(seconds)

    task = asyncio.get_running_loop().create_task(loop())
    ctx.on_close(task.cancel)
