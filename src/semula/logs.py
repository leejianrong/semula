"""Keep one-time secrets out of the access log (SEM-114).

The sign-in ``code`` and ``state`` come back in a callback's query string, and an invitation link
carries its token in the path. Both are single use and short lived, but an access log is kept,
shipped and read by people who should not be able to use them.
"""

from __future__ import annotations

import logging
import re

_QUERY = re.compile(
    r"([?&](?:code|state|sid|token|access_token|id_token|refresh_token|session)=)[^&#\s]*",
    re.IGNORECASE,
)
_INVITE = re.compile(r"(/auth/invite/)[^/?#\s]+")


def scrub(address: str) -> str:
    return _INVITE.sub(r"\1REDACTED", _QUERY.sub(r"\1REDACTED", address))


class AccessScrub(logging.Filter):
    """On uvicorn's access logger: the request line is the third argument."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], scrub(args[2]), *args[3:])
        return True


def install_access_scrub() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessScrub) for f in logger.filters):
        logger.addFilter(AccessScrub())
