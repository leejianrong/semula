from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from semula.app import create_app
from semula.config import Settings


def make_client(tmp_path: Path, **overrides: object) -> httpx.AsyncClient:
    settings = Settings(data_dir=tmp_path, **overrides)  # type: ignore[arg-type]
    app = create_app(settings)
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
