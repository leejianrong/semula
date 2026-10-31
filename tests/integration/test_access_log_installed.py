import logging
from pathlib import Path

import pytest

from semula.app import create_app
from semula.config import Settings
from semula.logs import AccessScrub
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration


def scrubbing() -> bool:
    return any(isinstance(f, AccessScrub) for f in logging.getLogger("uvicorn.access").filters)


def test_a_hosted_server_scrubs_its_access_log(tmp_path: Path) -> None:
    for f in [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, AccessScrub)]:
        logging.getLogger("uvicorn.access").removeFilter(f)
    create_app(Settings(data_dir=tmp_path, hosted=True, database_url=fresh_database_url(tmp_path)))
    assert scrubbing()
