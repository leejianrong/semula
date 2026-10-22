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
from collections.abc import Callable
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
        # With replicas (cluster.PgBridge): ``forward`` queues an event for the others and never
        # blocks; ``on_remote`` is told of each event that came from another replica.
        self.forward: Callable[[dict[str, Any]], None] | None = None
        self.on_remote: list[Callable[[dict[str, Any]], None]] = []

    def subscribe(self) -> Subscription:
        """Subscribe from inside a running event loop."""
        sub = Subscription(self, asyncio.get_running_loop())
        with self._lock:
            self._subs.append(sub)
        return sub

    @property
    def networked(self) -> bool:
        """Are there other replicas to hear from? Then waits also look again now and then."""
        return self.forward is not None

    def publish(self, event: dict[str, Any]) -> None:
        self.publish_local(event)
        if self.forward is not None:
            self.forward(event)

    def deliver_remote(self, event: dict[str, Any]) -> None:
        """An event another replica published: tell the local subscribers and hooks only."""
        self.publish_local(event)
        for hook in self.on_remote:
            hook(event)

    def publish_local(self, event: dict[str, Any]) -> None:
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
