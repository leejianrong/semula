"""A refused or repeated request must not cost a thread-pool hop (SEM-121).

Under a flood every request used to go through ``run_in_threadpool`` to check its token, even
with the token cached, so a flood from one token queued the thread pool every other workspace's
requests wait in. The cache is now read on the event loop, and the pool is used only on a miss.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from semula import auth
from semula.app import create_app
from semula.config import Settings
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration


@pytest.fixture
def hosted(tmp_path: Path) -> Iterator[Any]:
    settings = Settings(
        data_dir=tmp_path, hosted=True, secret="x" * 32, database_url=fresh_database_url(tmp_path)
    )
    yield create_app(settings)


async def test_a_cached_token_never_enters_the_thread_pool(
    hosted: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = hosted.state.service
    service.create_workspace("a")
    from semula.scope import Scope

    token = service.for_scope(Scope("a")).create_token("ci", "admin")["token"]
    calls: list[str] = []
    real = auth.run_in_threadpool

    async def counted(fn: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(getattr(fn, "__name__", str(fn)))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(auth, "run_in_threadpool", counted)
    transport = httpx.ASGITransport(app=hosted)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        headers = {"Authorization": f"Bearer {token}"}
        assert (await c.get("/api/v1/projects", headers=headers)).status_code == 200
        first = calls.count("authenticate_token")
        assert first == 1  # the miss goes to the pool
        for _ in range(5):
            assert (await c.get("/api/v1/projects", headers=headers)).status_code == 200
        assert calls.count("authenticate_token") == first  # the hits do not
        bad = {"Authorization": "Bearer smla_live_" + "x" * 32}
        assert (await c.get("/api/v1/projects", headers=bad)).status_code == 401
