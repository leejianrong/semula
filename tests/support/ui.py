"""Drive the dashboard like a browser tab: open its event stream, send events, wait for patches."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx


class Tab:
    """One open tab: an SSE connection to ``/ui/api/stream`` and the events it would post."""

    def __init__(
        self, url: str, path: str = "/projects/default/log", cookies: dict[str, str] | None = None
    ) -> None:
        self.url = url
        self.cookies = cookies or {}
        self.sid = uuid.uuid4().hex
        if self.cookies:  # a hosted dashboard: trade the sign-in for a signed session id
            resp = httpx.post(
                url + "/ui/api/session", json={}, cookies=self.cookies, headers={"Origin": url}
            )
            assert resp.status_code == 200, resp.text
            self.sid = resp.json()["token"]
        self.messages: list[dict[str, Any]] = []
        self.arrived: list[float] = []
        self._lock = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, args=(path,), daemon=True)
        self._thread.start()
        self.init = self.wait_for(lambda m: m["type"] == "init", 10)[0]

    def _read(self, path: str) -> None:
        params = {"sid": self.sid, "path": path}
        try:
            with httpx.stream(
                "GET",
                self.url + "/ui/api/stream",
                params=params,
                cookies=self.cookies,
                timeout=httpx.Timeout(5, read=60),
            ) as resp:
                for line in resp.iter_lines():
                    if self._stop.is_set():
                        return
                    if line.startswith("data:"):
                        with self._lock:
                            self.messages.append(json.loads(line[5:]))
                            self.arrived.append(time.perf_counter())
                            self._lock.notify_all()
        except httpx.HTTPError:
            pass

    def wait_for(
        self, found: Callable[[dict[str, Any]], bool], timeout: float = 3.0, after: int = 0
    ) -> tuple[dict[str, Any], float]:
        """The first message that ``found`` accepts, and the seconds waited. It looks at
        messages already received too; pass a ``mark()`` as ``after`` to look only past it."""
        started = time.perf_counter()
        seen = after
        with self._lock:
            while True:
                for message in self.messages[seen:]:
                    if found(message):
                        return message, time.perf_counter() - started
                seen = len(self.messages)
                left = timeout - (time.perf_counter() - started)
                if left <= 0:
                    raise TimeoutError("the tab never saw the message it was waiting for")
                self._lock.wait(left)

    def saw(self, text: str) -> bool:
        with self._lock:
            return any(text in json.dumps(m) for m in self.messages)

    def mark(self) -> int:
        with self._lock:
            return len(self.messages)

    def since(self, mark: int) -> str:
        with self._lock:
            return json.dumps(self.messages[mark:])

    def changes_to(self, node: dict[str, Any], mark: int = 0) -> str:
        """Only the patch changes aimed at ``node`` since ``mark``: every screen of the dashboard
        is live in every tab, so a bare text search also finds other screens' updates."""
        with self._lock:
            hits = [
                c
                for m in self.messages[mark:]
                if m["type"] == "patch"
                for c in m["changes"]
                if c.get("target") == node["id"]
            ]
        return json.dumps(hits)

    def nodes(self) -> Iterator[dict[str, Any]]:
        stack = [self.init["root"]]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(node.get("children") or [])

    def find(self, type_: str, **props: Any) -> dict[str, Any]:
        for node in self.nodes():
            if node["type"] == type_ and all(
                node.get("props", {}).get(k) == v for k, v in props.items()
            ):
                return node
        raise LookupError(f"no {type_} with {props}")

    def send(self, node: dict[str, Any], event: str, payload: dict[str, Any] | None = None) -> None:
        body = {"sid": self.sid, "component": node["id"], "event": event, "payload": payload or {}}
        resp = httpx.post(
            self.url + "/ui/api/event",
            json=body,
            cookies=self.cookies,
            headers={"Origin": self.url} if self.cookies else {},
            timeout=5,
        )
        assert resp.status_code == 200, resp.text

    def close(self) -> None:
        self._stop.set()
