"""Group calls whose messages extend an earlier call's (SEM-98).

An agent loop resends a growing history, so each call looks unrelated to the one before. A call
belongs to the conversation of the earlier call whose messages are the longest prefix of its own
(with the same tools). That is all: no semantic grouping, so two prompts that merely look alike
stay apart.
"""

from __future__ import annotations

import json
from typing import Any


def _key(message: Any) -> str:
    return json.dumps(message, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def thread(calls: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]]) -> dict[str, Any]:
    """Place each ``(id, messages, tools)`` in a conversation. Ids sort oldest first (ULIDs do).

    Returns ``{id: {"parent", "root", "depth", "added", "repeat"}}``: the call it continues (or
    None), the first call of its conversation, how many calls deep it is, the messages it adds to
    its parent (all of them when it has none, none for an identical repeat), and whether it
    repeats its parent exactly.
    """
    seen: dict[tuple[str, tuple[str, ...]], str] = {}
    placed: dict[str, dict[str, Any]] = {}
    for call_id, messages, tools in sorted(calls, key=lambda c: c[0]):
        tool_key = _key(tools)
        keys = tuple(_key(m) for m in messages)
        parent: str | None = None
        matched = 0
        for length in range(len(keys), 0, -1):
            found = seen.get((tool_key, keys[:length]))
            if found is not None:
                parent, matched = found, length
                break
        if parent is None:
            node = {"parent": None, "root": call_id, "depth": 1, "added": messages, "repeat": False}
        else:
            above = placed[parent]
            node = {
                "parent": parent,
                "root": above["root"],
                "depth": above["depth"] + 1,
                "added": messages[matched:],
                "repeat": matched == len(keys),
            }
        placed[call_id] = node
        seen.setdefault((tool_key, keys), call_id)
    return placed
