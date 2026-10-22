"""Share events between replicas on one Postgres with LISTEN/NOTIFY (ADR-0011 section 8).

Each replica keeps its in-process bus. What it publishes is also queued for a sender thread that
runs ``pg_notify``, so publishing never waits for the network (a replay hit publishes a call
event, and the hit path has a latency budget). A listener thread turns other replicas' events into
local ones. A replica ignores its own notifications, so nobody hears an event twice. Delivery is
best effort: the held-call wait and the pending-list wait also look again every second, which is
what makes a lost notification harmless.

Postgres only; psycopg is imported here and nowhere on the default path.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .events import EventBus

log = logging.getLogger("semula.cluster")
NOTIFY_LIMIT = 7000  # Postgres refuses a payload over 8000 bytes
QUEUE_LIMIT = 5000
POLL_SECONDS = 1.0  # how long the listener waits before looking at its stop flag


def to_dsn(url: str) -> str:
    """A libpq connection string from a SQLAlchemy URL (the driver suffix removed)."""
    import sqlalchemy as sa

    from .db import normalise_url

    parsed = sa.make_url(normalise_url(url)).set(drivername="postgresql")
    return parsed.render_as_string(hide_password=False)


class PgBridge:
    def __init__(self, url: str, bus: EventBus, channel: str = "semula_events") -> None:
        self.dsn = to_dsn(url)
        self.bus = bus
        self.channel = channel
        self.replica = uuid.uuid4().hex
        self._outbox: queue.Queue[str] = queue.Queue(QUEUE_LIMIT)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._threads: list[threading.Thread] = []

    # --- lifecycle --------------------------------------------------------------------
    def start(self) -> None:
        for target in (self._listen, self._send):
            thread = threading.Thread(target=target, name=f"semula-{target.__name__}", daemon=True)
            thread.start()
            self._threads.append(thread)
        self._ready.wait(5)  # listening before the first event is published

    def stop(self) -> None:
        self._stop.set()
        self._outbox.put_nowait("")  # wake the sender
        for thread in self._threads:
            thread.join(timeout=3)

    # --- out --------------------------------------------------------------------------
    def forward(self, event: dict[str, Any]) -> None:
        """Queue ``event`` for the other replicas. Never blocks and never raises."""
        payload = json.dumps({"from": self.replica, "event": event}, separators=(",", ":"))
        if len(payload) > NOTIFY_LIMIT:  # a call preview is the only thing that grows
            payload = json.dumps(
                {"from": self.replica, "event": {**event, "preview": None}}, separators=(",", ":")
            )
        with contextlib.suppress(queue.Full):  # the waiters' poll covers what a full queue drops
            self._outbox.put_nowait(payload)

    def _send(self) -> None:
        import psycopg

        conn: psycopg.Connection[Any] | None = None
        while not self._stop.is_set():
            payload = self._outbox.get()
            if not payload:
                continue
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(self.dsn, autocommit=True)
                conn.execute("SELECT pg_notify(%s, %s)", (self.channel, payload))
            except Exception:  # a dropped connection: reconnect on the next event
                log.warning("could not notify the other replicas", exc_info=True)
                if conn is not None:
                    conn.close()
                conn = None
        if conn is not None:
            conn.close()

    # --- in ---------------------------------------------------------------------------
    def _listen(self) -> None:
        import psycopg

        while not self._stop.is_set():
            try:
                with psycopg.connect(self.dsn, autocommit=True) as conn:
                    conn.execute(f'LISTEN "{self.channel}"')
                    self._ready.set()
                    for note in conn.notifies(timeout=POLL_SECONDS):
                        self._receive(note.payload)
                        if self._stop.is_set():
                            return
                    while not self._stop.is_set():  # notifies() returns when the timeout lapses
                        for note in conn.notifies(timeout=POLL_SECONDS):
                            self._receive(note.payload)
            except Exception:
                log.warning("lost the connection to the other replicas; retrying", exc_info=True)
                self._ready.set()
                self._stop.wait(2)

    def _receive(self, payload: str) -> None:
        try:
            message = json.loads(payload)
            if message["from"] == self.replica:
                return
            self.bus.deliver_remote(message["event"])
        except Exception:
            log.warning("ignored a malformed event from another replica", exc_info=True)
