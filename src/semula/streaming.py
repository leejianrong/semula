"""Server-Sent Events for stream: true (ADR-0008).

A stored entry replays as stored chunks when it has them, else the assembled
message is re-chunked. Either way the wire shape is what OpenAI SDK stream
iterators expect: a role chunk, content/tool-call deltas, a finish chunk, an
optional usage chunk, then ``data: [DONE]``.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any


def _pieces(text: str, size: int) -> Iterator[str]:
    size = max(1, size)
    for i in range(0, len(text), size):
        yield text[i : i + size]


def rechunk(message: Mapping[str, Any], chunk_size: int) -> list[dict[str, Any]]:
    """Split an assembled assistant message into stream deltas."""
    deltas: list[dict[str, Any]] = []
    content = message.get("content")
    if content:
        deltas.extend({"content": p} for p in _pieces(content, chunk_size))
    for index, call in enumerate(message.get("tool_calls") or []):
        fn = call["function"]
        deltas.append(
            {
                "tool_calls": [
                    {
                        "index": index,
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": fn["name"], "arguments": ""},
                    }
                ]
            }
        )
        deltas.extend(
            {"tool_calls": [{"index": index, "function": {"arguments": p}}]}
            for p in _pieces(fn.get("arguments") or "", chunk_size)
        )
    return deltas


def assemble(deltas: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Rebuild one assistant message from stream deltas."""
    content_parts: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    saw_content = False
    for delta in deltas:
        if delta.get("content") is not None:
            saw_content = True
            content_parts.append(str(delta["content"]))
        for tc in delta.get("tool_calls") or []:
            slot = calls.setdefault(
                int(tc.get("index", 0)),
                {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            slot["function"]["name"] += fn.get("name") or ""
            slot["function"]["arguments"] += fn.get("arguments") or ""
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) if saw_content else None,
    }
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return message


def ensure_tool_call_ids(
    deltas: list[dict[str, Any]], calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Copy ids generated during validation back into the first delta of each call."""
    seen: set[int] = set()
    for delta in deltas:
        for tc in delta.get("tool_calls") or []:
            index = int(tc.get("index", 0))
            if index in seen:
                continue
            seen.add(index)
            if not tc.get("id") and index < len(calls):
                tc["id"] = calls[index]["id"]
    return deltas


def _chunk(
    cid: str,
    created: int,
    model: str,
    delta: Mapping[str, Any],
    finish: str | None,
    usage: Mapping[str, Any] | None = None,
) -> str:
    body: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": []
        if usage is not None
        else [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    return f"data: {json.dumps(body, separators=(',', ':'))}\n\n"


def sse_events(
    stored: Mapping[str, Any],
    model: str,
    *,
    chunk_size: int,
    include_usage: bool,
) -> Iterator[str]:
    cid = f"chatcmpl-{secrets.token_hex(12)}"
    created = int(time.time())
    message = stored["message"]
    deltas = stored.get("chunks") or rechunk(message, chunk_size)
    first = deltas[0] if deltas else {}
    if "role" not in first:
        yield _chunk(cid, created, model, {"role": "assistant", "content": ""}, None)
    for delta in deltas:
        yield _chunk(cid, created, model, delta, None)
    yield _chunk(cid, created, model, {}, stored["finish_reason"])
    if include_usage:
        usage = stored.get("usage") or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        yield _chunk(cid, created, model, {}, None, usage)
    yield "data: [DONE]\n\n"
