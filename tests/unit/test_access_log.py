"""One-time secrets that travel in the address must not reach the access log (SEM-114): the
sign-in ``code`` and ``state`` in a callback, and an invitation token in its path."""

import logging

import pytest

from semula.logs import AccessScrub, install_access_scrub

pytestmark = pytest.mark.unit


def access_line(path: str) -> str:
    """What uvicorn logs: ``client - "METHOD path HTTP/1.1" status``."""
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "x", 1,
        '%s - "%s %s HTTP/%s" %d', ("203.0.113.9:5000", "GET", path, "1.1", 302), None,
    )  # fmt: skip
    assert AccessScrub().filter(record)
    return record.getMessage()


@pytest.mark.parametrize(
    ("path", "gone"),
    [
        ("/auth/callback/google?code=SECRETCODE&state=SECRETSTATE", ["SECRETCODE", "SECRETSTATE"]),
        ("/auth/callback/github?state=SECRETSTATE&code=SECRETCODE&error=x", ["SECRETCODE"]),
        ("/auth/invite/smla_inv_SECRETINVITE", ["SECRETINVITE"]),
        ("/auth/invite/smla_inv_SECRETINVITE?next=/ui", ["SECRETINVITE"]),
        ("/x?access_token=SECRETTOKEN&id_token=SECRETID", ["SECRETTOKEN", "SECRETID"]),
        ("/ui/api/stream?sid=SECRETSID&path=%2Fprojects", ["SECRETSID"]),
    ],
)
def test_a_secret_in_the_address_is_not_logged(path: str, gone: list[str]) -> None:
    line = access_line(path)
    for secret in gone:
        assert secret not in line, line


def test_everything_else_about_the_request_is_kept() -> None:
    line = access_line("/auth/callback/google?code=SECRETCODE&state=s&error=access_denied")
    assert "203.0.113.9:5000" in line and "GET" in line and "302" in line
    assert "/auth/callback/google" in line and "error=access_denied" in line
    assert access_line("/v1/models") == '203.0.113.9:5000 - "GET /v1/models HTTP/1.1" 302'


def test_install_puts_the_filter_on_uvicorns_access_logger_once() -> None:
    logger = logging.getLogger("uvicorn.access")
    for f in [f for f in logger.filters if isinstance(f, AccessScrub)]:
        logger.removeFilter(f)
    install_access_scrub()
    install_access_scrub()
    assert sum(isinstance(f, AccessScrub) for f in logger.filters) == 1
