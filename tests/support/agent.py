"""A stand-in coding agent: answers the next pending request through the admin API."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx


@contextmanager
def agent_answers(url: str, answer: Any) -> Iterator[None]:
    """While active, answer the first pending request that appears with ``answer``."""
    done = threading.Event()
    failure: list[str] = []

    def run() -> None:
        for _ in range(1000):
            pending = httpx.get(url + "/api/v1/pending").json()["pending"]
            if pending:
                resp = httpx.post(f"{url}/api/v1/pending/{pending[0]['id']}/answer", json=answer)
                if resp.status_code != 200:
                    failure.append(resp.text)
                done.set()
                return
            time.sleep(0.02)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        thread.join(timeout=15)
    assert done.is_set(), "no request ever became pending"
    assert not failure, f"the answer was rejected: {failure[0]}"
