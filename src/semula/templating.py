"""Replay-time templates in stored responses.

Verbatim replay breaks assertions on time and IDs, so a stored response may contain
``{{now}}``, ``{{uuid}}`` and ``{{request.model}}``. Anything else in braces is left
exactly as written. Chunks are expanded one by one, so a template split across two
recorded chunks is not expanded; keep a template inside one chunk.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

_TEMPLATE = re.compile(r"\{\{\s*(now|uuid|request\.model)\s*\}\}")


def expand_text(text: str, model: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "now":
            return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if name == "uuid":
            return str(uuid.uuid4())
        return model

    return _TEMPLATE.sub(replace, text)


def expand(value: Any, model: str) -> Any:
    if isinstance(value, str):
        return expand_text(value, model)
    if isinstance(value, list):
        return [expand(v, model) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, model) for k, v in value.items()}
    return value


def expand_stored(stored: dict[str, Any], model: str) -> dict[str, Any]:
    """Expand the message and chunks of a stored response; leave usage and finish alone."""
    out = dict(stored)
    out["message"] = expand(stored["message"], model)
    if stored.get("chunks"):
        out["chunks"] = expand(stored["chunks"], model)
    return out
