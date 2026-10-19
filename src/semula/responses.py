"""Stored responses: validation of agent answers and rebuilding a completion.

An entry stores the assembled assistant message, finish reason and usage
(ADR-0008), not a whole completion. ``id``, ``created`` and ``model`` are created
fresh at replay so a stored answer never leaks a stale identity.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from typing import Any

from .errors import SemulaError
from .streaming import assemble, ensure_tool_call_ids

FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter", "function_call"}


class AnswerInvalid(SemulaError):
    def __init__(self, message: str, param: str | None = None) -> None:
        super().__init__(422, "invalid_answer", message, param=param)


def parse_answer(payload: Any) -> dict[str, Any]:
    """Validate an agent's answer and return the stored form.

    Accepts a full Chat Completions body (uses ``choices[0]``), a
    ``{"message": {...}}`` object, or the shorthand ``{"content": "text"}``.
    """
    if isinstance(payload, list):
        return _from_chunks(payload, None, None)
    if not isinstance(payload, Mapping):
        raise AnswerInvalid("The answer must be a JSON object or a list of stream chunks.")
    if isinstance(payload.get("chunks"), list):
        return _from_chunks(payload["chunks"], payload.get("finish_reason"), payload.get("usage"))
    if "choices" in payload:
        choices = payload["choices"]
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise AnswerInvalid("'choices' must be a non-empty list of objects.", "choices")
        choice: Mapping[str, Any] = choices[0]
        message = choice.get("message")
        finish = choice.get("finish_reason")
        usage = payload.get("usage")
        if message is None:
            raise AnswerInvalid("choices[0].message is required.", "choices[0].message")
    elif "message" in payload:
        message, finish, usage = (
            payload["message"],
            payload.get("finish_reason"),
            payload.get("usage"),
        )
    elif "content" in payload:
        message = {"role": "assistant", "content": payload["content"]}
        finish, usage = payload.get("finish_reason"), payload.get("usage")
    else:
        raise AnswerInvalid(
            "Send a chat completion body, {'message': {...}}, or {'content': 'text'}."
        )
    return _validate_message(message, finish, usage)


def _from_chunks(chunks: list[Any], finish: Any, usage: Any) -> dict[str, Any]:
    """Accept an answer recorded as a stream, keeping its chunk boundaries (ADR-0008).

    Each item is a plain string (a content piece), a bare delta object, or a full
    ``chat.completion.chunk``. Boundaries are kept so replay can emit them verbatim.
    """
    deltas: list[dict[str, Any]] = []
    for i, item in enumerate(chunks):
        if isinstance(item, str):
            deltas.append({"content": item})
        elif isinstance(item, Mapping) and "choices" in item:
            if item.get("usage") and usage is None:
                usage = item["usage"]
            choices = item["choices"]
            if choices:
                choice = choices[0]
                finish = choice.get("finish_reason") or finish
                if choice.get("delta"):
                    deltas.append(dict(choice["delta"]))
        elif isinstance(item, Mapping):
            deltas.append(dict(item))
        else:
            raise AnswerInvalid("Each chunk must be a string or an object.", f"chunks[{i}]")
    deltas = [d for d in deltas if d]
    if not deltas:
        raise AnswerInvalid("The stream has no content or tool-call deltas.", "chunks")
    message = assemble(deltas)
    stored = _validate_message(message, finish, usage)
    stored["chunks"] = ensure_tool_call_ids(deltas, stored["message"].get("tool_calls") or [])
    return stored


def _validate_message(message: Any, finish: Any, usage: Any) -> dict[str, Any]:
    if not isinstance(message, Mapping):
        raise AnswerInvalid("The message must be a JSON object.", "message")
    if message.get("role", "assistant") != "assistant":
        raise AnswerInvalid("The message role must be 'assistant'.", "message.role")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise AnswerInvalid("message.content must be a string or null.", "message.content")
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        tool_calls = _validate_tool_calls(tool_calls)
    if content is None and not tool_calls:
        raise AnswerInvalid("The message needs content or tool_calls.", "message")
    if finish is None:
        finish = "tool_calls" if tool_calls else "stop"
    if finish not in FINISH_REASONS:
        raise AnswerInvalid(
            f"finish_reason must be one of {sorted(FINISH_REASONS)}, got {finish!r}.",
            "finish_reason",
        )
    if usage is not None and not isinstance(usage, Mapping):
        raise AnswerInvalid("usage must be an object.", "usage")
    out_message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        out_message["tool_calls"] = tool_calls
    return {
        "message": out_message,
        "finish_reason": finish,
        "usage": dict(usage) if usage else None,
    }


def _validate_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        raise AnswerInvalid("tool_calls must be a list.", "message.tool_calls")
    out: list[dict[str, Any]] = []
    for i, call in enumerate(tool_calls):
        where = f"message.tool_calls[{i}]"
        if not isinstance(call, Mapping) or not isinstance(call.get("function"), Mapping):
            raise AnswerInvalid("Each tool call needs a 'function' object.", where)
        fn = call["function"]
        if not isinstance(fn.get("name"), str) or not fn["name"]:
            raise AnswerInvalid(
                "function.name must be a non-empty string.", f"{where}.function.name"
            )
        args = fn.get("arguments", "{}")
        if isinstance(args, Mapping):
            args = json.dumps(args)
        if not isinstance(args, str):
            raise AnswerInvalid(
                "function.arguments must be a JSON string.", f"{where}.function.arguments"
            )
        try:
            json.loads(args)
        except ValueError as exc:
            raise AnswerInvalid(
                f"function.arguments is not valid JSON: {exc}", f"{where}.function.arguments"
            ) from exc
        out.append(
            {
                "id": call.get("id") or f"call_{secrets.token_hex(8)}",
                "type": "function",
                "function": {"name": fn["name"], "arguments": args},
            }
        )
    return out


def build_completion(stored: Mapping[str, Any], model: str) -> dict[str, Any]:
    usage = stored.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "id": f"chatcmpl-{secrets.token_hex(12)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": stored["message"], "finish_reason": stored["finish_reason"]}
        ],
        "usage": usage,
    }
