from collections.abc import Iterator

import pytest

from semula.db import Database
from tests.support.db import drop_created_schemas


@pytest.fixture(scope="session", autouse=True)
def _drop_test_schemas() -> Iterator[None]:
    yield
    drop_created_schemas()


@pytest.fixture(autouse=True)
def _close_databases(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Dispose every engine a test opened, so a Postgres run never exhausts its connections."""
    opened: list[Database] = []
    original = Database.__init__

    def tracking(self: Database, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(self)

    monkeypatch.setattr(Database, "__init__", tracking)
    yield
    for db in opened:
        db.close()


def pytest_report_header(config: pytest.Config) -> str:
    import os

    url = os.environ.get("SEMULA_TEST_DATABASE_URL")
    return f"semula test database: {'postgresql' if url else 'sqlite'}"
