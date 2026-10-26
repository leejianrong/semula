import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from semula.app import create_app
from semula.config import Settings
from tests.support.db import fresh_database_url


def make_client(tmp_path: Path, **overrides: object) -> httpx.AsyncClient:
    overrides.setdefault("database_url", fresh_database_url(tmp_path))
    embedder = overrides.pop("embedder", None)  # a fake, so the fast layer needs no model
    settings = Settings(data_dir=tmp_path, **overrides)  # type: ignore[arg-type]
    app = create_app(settings, embedder)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.fixture
async def replay(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(tmp_path, mode="replay") as client:
        yield client


@pytest.fixture
async def record(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(tmp_path, mode="record", record_timeout=5.0) as client:
        yield client


def chat(content: str = "hello", **extra: object) -> dict[str, object]:
    return {"model": "gpt-test", "messages": [{"role": "user", "content": content}], **extra}


async def parked(
    client: httpx.AsyncClient, project: str | None = None, count: int = 1, seconds: float = 10.0
) -> list[dict]:
    """The parked calls once ``count`` of them have parked. A fixed sleep is a race against a slow
    runner or database; this polls, and fails with a clear message if none ever parks."""
    params = {"project": project} if project else {}
    pending: list[dict] = []
    for _ in range(int(seconds / 0.02)):
        pending = (await client.get("/api/v1/pending", params=params)).json()["pending"]
        if len(pending) >= count:
            return pending
        await asyncio.sleep(0.02)
    raise AssertionError(f"expected {count} parked call(s), found {len(pending)} after {seconds}s")


def slow_parking(client: httpx.AsyncClient, seconds: float) -> None:
    """Make parking a call take ``seconds``, as it does on a slow runner or database."""
    service = client._transport.app.state.service  # type: ignore[attr-defined]
    real = service.open_pending

    def slow(*args: object, **kwargs: object) -> object:
        time.sleep(seconds)
        return real(*args, **kwargs)

    service.open_pending = slow
