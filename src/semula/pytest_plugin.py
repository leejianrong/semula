"""The pytest plugin: a ``semula`` fixture that starts a throwaway server and fails the test
on any call it had no recorded answer for.

    def test_summary(semula):          # OPENAI_BASE_URL now points at semula
        assert summarise("...") == "..."

Loaded by pytest on its own once semula is installed (the ``pytest11`` entry point). Nothing
heavy is imported until a test asks for the fixture.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from .ephemeral import Ephemeral


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("semula")
    group.addoption(
        "--semula-store", default=None, metavar="DIR",
        help="keep semula's recordings in this directory (default: a throwaway one)",
    )  # fmt: skip
    group.addoption(
        "--semula-record", action="store_true", default=False,
        help="record: park calls with no answer for an agent instead of failing the test",
    )  # fmt: skip
    parser.addini("semula_store", "directory that holds semula's recordings", default="")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "semula_allow_misses: do not fail this test on a call with no recorded answer"
    )


class Semula:
    """What the ``semula`` fixture gives a test."""

    def __init__(self, server: Ephemeral, since: str) -> None:
        self._server = server
        self._since = since

    @property
    def url(self) -> str:
        return self._server.url

    @property
    def base_url(self) -> str:
        """What an OpenAI client takes as its base URL (also in OPENAI_BASE_URL)."""
        return self._server.base_url

    def misses(self) -> dict[str, object]:
        """The miss report for this test so far (``GET /api/v1/check``)."""
        return self._server.check(self._since)

    def assert_no_misses(self) -> None:
        from .ephemeral import describe_misses

        report = self._server.check(self._since)
        if not report["ok"]:
            pytest.fail(describe_misses(report), pytrace=False)


@pytest.fixture(scope="session")
def semula_server(request: pytest.FixtureRequest) -> Iterator[Ephemeral]:
    """One server for the whole session, so each test starts in milliseconds."""
    from .ephemeral import ephemeral_server

    config = request.config
    store = config.getoption("semula_store") or config.getini("semula_store") or None
    record = config.getoption("semula_record")
    with ephemeral_server(store, "record" if record else "replay") as server:
        yield server


@pytest.fixture
def semula(
    semula_server: Ephemeral, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Semula]:
    """Point the code under test at semula; after the test, fail it on any miss."""
    from .ephemeral import now

    monkeypatch.setenv("OPENAI_BASE_URL", semula_server.base_url)
    if not os.environ.get("OPENAI_API_KEY"):
        monkeypatch.setenv("OPENAI_API_KEY", "semula")
    handle = Semula(semula_server, now())
    yield handle
    if semula_server.mode != "record" and not request.node.get_closest_marker(
        "semula_allow_misses"
    ):
        handle.assert_no_misses()
