"""ULID generation (sortable 26-char IDs), with no dependency."""

from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS = 80
_lock = threading.Lock()
_last_ms = -1
_last_random = 0


def _encode(value: int) -> str:
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(now_ms: int | None = None) -> str:
    """A new id. With no ``now_ms`` the ids of one process are strictly increasing, as the ULID
    spec allows: two made in the same millisecond (or after the clock stepped back) still sort in
    the order they were made, which "oldest first" listings and conversation grouping rely on.
    An explicit ``now_ms`` is used exactly as given, for ids placed at a chosen time."""
    if now_ms is not None:
        return _encode((now_ms << _RANDOM_BITS) | int.from_bytes(os.urandom(10), "big"))
    global _last_ms, _last_random
    with _lock:
        ms = int(time.time() * 1000)
        if ms > _last_ms:
            _last_ms, _last_random = ms, int.from_bytes(os.urandom(10), "big")
        else:  # same millisecond, or the clock went back: carry on from the last id
            _last_random += 1
            if _last_random >> _RANDOM_BITS:  # 2**80 ids in a millisecond: move on one
                _last_ms, _last_random = _last_ms + 1, 0
        return _encode((_last_ms << _RANDOM_BITS) | _last_random)
