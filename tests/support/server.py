"""Run the real app on a loopback port in a thread, for tests that need real HTTP."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn

from semula.app import create_app
from semula.config import Settings
from tests.support.db import fresh_database_url


@contextmanager
def running_server(data_dir: Path, **settings: object) -> Iterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    settings.setdefault("database_url", fresh_database_url(data_dir))
    app = create_app(Settings(data_dir=data_dir, **settings))  # type: ignore[arg-type]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            httpx.get(url + "/health", timeout=0.2)
            break
        except httpx.HTTPError:
            time.sleep(0.02)
    else:
        raise RuntimeError("server did not start")
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
