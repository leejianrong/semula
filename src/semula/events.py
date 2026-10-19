"""An in-process event bus for live views (SEM-35).

``publish`` may be called from any thread (the service runs in worker threads) and never
blocks or fails: each subscriber has a small bounded queue on its own event loop, and when a
slow consumer falls behind its oldest events are dropped and counted. The proxy path
therefore cannot be slowed down by a stuck reader.

Single process only. A hosted, multi-replica deployment needs a shared channel (ADR-0009).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

QUEUE_SIZE = 256


class Subscription:
    def __init__(self, bus: EventBus, loop: asyncio.AbstractEventLoop) -> None:
        self._bus = bus
        self._loop = loop
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(QUEUE_SIZE)
        self.dropped = 0

    def _offer(self, event: dict[str, Any]) -> None:  # runs on the subscriber's loop
        while True:
            try:
                self.queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                self.queue.get_nowait()  # shed the oldest, keep the newest
                self.dropped += 1

    async def get(self) -> dict[str, Any]:
        return await self.queue.get()

    def close(self) -> None:
        self._bus._remove(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()

    def subscribe(self) -> Subscription:
        """Subscribe from inside a running event loop."""
        sub = Subscription(self, asyncio.get_running_loop())
        with self._lock:
            self._subs.append(sub)
        return sub

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                sub._loop.call_soon_threadsafe(sub._offer, event)
            except RuntimeError:  # its loop has closed: the subscriber is gone
                self._remove(sub)

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)
