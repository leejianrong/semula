from collections.abc import Iterator

import pytest

from tests.support.db import drop_created_schemas


@pytest.fixture(scope="session", autouse=True)
def _drop_test_schemas() -> Iterator[None]:
    yield
    drop_created_schemas()
