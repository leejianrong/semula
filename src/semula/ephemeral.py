"""A throwaway semula server on a free loopback port, for `semula run` and the pytest plugin.

It is the real app (the same ``create_app``) in a thread, on a temporary directory unless a
store is named. Nothing here is hosted: no API key, no sign-in.
"""

from __future__ import annotations

import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import uvicorn

from .app import create_app
from .config import Settings
from .schema import iso

Mode = Literal["replay", "strict", "record"]


@dataclass(frozen=True)
class Ephemeral:
    url: str
    mode: Mode

    @property
    def base_url(self) -> str:
        """What an OpenAI client takes as its base URL."""
        return self.url + "/v1"

    def check(self, since: str | None = None, project: str | None = None) -> dict[str, Any]:
        """The miss report (``GET /api/v1/check``) for the run that began at ``since``."""
        params = {k: v for k, v in (("since", since), ("project", project)) if v}
        resp = httpx.get(self.url + "/api/v1/check", params=params, timeout=30.0)
        resp.raise_for_status()
        report: dict[str, Any] = resp.json()
        return report


def now() -> str:
    return iso(datetime.now(UTC))


def describe_misses(report: dict[str, Any]) -> str:
    """The failure message for a report with misses: each call and its nearest entry."""
    lines = [f"{report['misses']} call(s) had no recorded answer ({report['calls']} made):"]
    for m in report["missed"]:
        near = m["nearest"]
        hint = f"nearest {near['entry_id']} ({near['score']:.2f})" if near else "no entries yet"
        lines.append(f"  {m['outcome']}: {m['preview'] or '(no preview)'}  [{hint}]")
    lines.append("Record them with `semula run --mode record -- <your command>`, then rerun.")
    return "\n".join(lines)


@contextmanager
def ephemeral_server(
    store: str | Path | None = None, mode: Mode = "replay", port: int | None = None
) -> Iterator[Ephemeral]:
    """Start the server, yield it, stop it. ``store`` keeps recordings in that directory;
    without it they live in a temporary one that is removed on exit."""
    scratch = None if store else tempfile.TemporaryDirectory(prefix="semula-run-")
    data_dir = Path(store) if store else Path(scratch.name)  # type: ignore[union-attr]
    data_dir.mkdir(parents=True, exist_ok=True)
    sock = socket.socket()
    server = None
    thread = None
    try:
        sock.bind(("127.0.0.1", port or 0))
        sock.listen(128)
        bound = sock.getsockname()[1]
        url = f"http://127.0.0.1:{bound}"
        settings = replace(
            Settings.from_env(),
            data_dir=data_dir,
            database_url=None,
            mode="record" if mode == "record" else "replay",
            host="127.0.0.1",
            port=bound,
            api_key=None,
            hosted=False,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings),
                host="127.0.0.1",
                port=bound,
                log_level="warning",
                proxy_headers=False,
            )
        )
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        for _ in range(500):
            try:
                httpx.get(url + "/health", timeout=0.5)
                break
            except httpx.HTTPError:
                time.sleep(0.02)
        else:
            raise RuntimeError("the semula server did not start")
        yield Ephemeral(url=url, mode=mode)
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        sock.close()
        if scratch is not None:
            scratch.cleanup()
