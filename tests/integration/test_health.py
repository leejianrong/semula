from pathlib import Path

import httpx
import pytest

from semula import __version__
from semula.app import create_app
from semula.config import Settings

pytestmark = pytest.mark.integration


async def test_health_reports_version_and_mode(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__, "mode": "replay"}
