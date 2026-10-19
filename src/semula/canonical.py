"""The canonical request and its fingerprints (ADR-0007).

Everything the matcher sees is a ``CanonicalRequest``, never the wire body, so a
Responses or Anthropic adapter later only has to produce one of these.

Two fingerprints are computed:

* ``exact``: the messages and tools as sent. Tier 1.
* ``normalised``: the same after scrubbing timestamps, UUIDs and absolute paths
  and collapsing whitespace. Tier 2.

``model``, ``temperature``, ``top_p``, ``max_tokens``, ``stream``, ``user`` and
``seed`` are never read, so they are ignored by construction (ADR-0007 default).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_UNIX_PATH = re.compile(r"(?<![\w:/.])/(?:[\w.@+~-]+/)+[\w.@+~-]+")
_WIN_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s\"'<>|]+\\)*[^\\\s\"'<>|]+")
_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class FingerprintConfig:
    """Per-project matching settings (ADR-0007). Defaults apply until V3 exposes them."""

    tail_n: int | None = None
    extra_scrubs: tuple[str, ...] = ()
    include_params: tuple[str, ...] = ()


IGNORABLE_PARAMS = ("model", "temperature", "top_p", "max_tokens", "stream", "user", "seed")


@dataclass(frozen=True)
class CanonicalRequest:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return _dumps({"messages": self.messages, "tools": self.tools, "params": self.params})

    @classmethod
    def from_json(cls, raw: str) -> CanonicalRequest:
        data = json.loads(raw)
        return cls(
            messages=data["messages"], tools=data.get("tools", []), params=data.get("params", {})
        )


def scrub(text: str, config: FingerprintConfig | None = None) -> str:
    """Replace values that change between runs of the same logical prompt."""
    text = _UUID.sub("[UUID]", text)
    text = _TIMESTAMP.sub("[TIMESTAMP]", text)
    text = _WIN_PATH.sub("[FILE_PATH]", text)
    text = _UNIX_PATH.sub("[FILE_PATH]", text)
    for pattern in config.extra_scrubs if config else ():
        text = re.sub(pattern, "[SCRUBBED]", text)
    return _SPACE.sub(" ", text).strip()


def from_openai(body: Mapping[str, Any]) -> CanonicalRequest:
    """Build a canonical request from a Chat Completions body."""
    messages = [_message(m) for m in body.get("messages", [])]
    tools = [_canonical_json(t) for t in body.get("tools") or []]
    tools.sort(key=_tool_name)
    params = {k: body[k] for k in IGNORABLE_PARAMS if k in body}
    return CanonicalRequest(messages=messages, tools=tools, params=params)


def exact_fingerprint(req: CanonicalRequest, config: FingerprintConfig | None = None) -> str:
    return _digest(_payload(req, _tail(req.messages, config), config))


def normalised_fingerprint(req: CanonicalRequest, config: FingerprintConfig | None = None) -> str:
    messages = [
        {**m, "content": scrub(m["content"], config)} if isinstance(m.get("content"), str) else m
        for m in _tail(req.messages, config)
    ]
    return _digest(_payload(req, messages, config))


def lexical_tokens(req: CanonicalRequest, config: FingerprintConfig | None = None) -> list[str]:
    """Lower-cased words of the scrubbed message text, in order, for lexical matching.

    Uses the same scrubbers and ``tail_n`` as the fingerprints, so noise that is ignored when
    hashing is ignored when indexing too. Identifiers such as ``user_id`` stay one token.
    """
    words: list[str] = []
    for message in _tail(req.messages, config):
        content = message.get("content")
        if isinstance(content, str):
            words.extend(_WORD.findall(scrub(content, config).lower()))
    return words


def shape(req: CanonicalRequest, config: FingerprintConfig | None = None) -> str:
    """The conversation's skeleton: who speaks in what order, which tools are on offer, and the
    request params the project says must match.

    Two requests with different shapes never share a lexical auto-serve, however alike their words.
    """
    roles = [m.get("role", "") for m in _tail(req.messages, config)]
    params = {k: req.params.get(k) for k in sorted(config.include_params)} if config else {}
    return _dumps({"roles": roles, "tools": req.tools, "params": params})


def _payload(
    req: CanonicalRequest, messages: list[dict[str, Any]], config: FingerprintConfig | None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"messages": messages, "tools": req.tools}
    if config and config.include_params:
        payload["params"] = {k: req.params.get(k) for k in sorted(config.include_params)}
    return payload


def _tail(messages: list[dict[str, Any]], config: FingerprintConfig | None) -> list[dict[str, Any]]:
    if config and config.tail_n:
        return messages[-config.tail_n :]
    return messages


def _message(raw: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"role": raw.get("role", ""), "content": _content(raw.get("content"))}
    for key in ("name", "tool_call_id"):
        if raw.get(key) is not None:
            out[key] = raw[key]
    if raw.get("tool_calls"):
        out["tool_calls"] = [_canonical_json(c) for c in raw["tool_calls"]]
    return out


def _content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, Mapping) and part.get("type") == "text":
            parts.append(str(part.get("text", "")))
        elif isinstance(part, Mapping):
            parts.append(f"[{part.get('type', 'part')}]")
        else:
            parts.append(str(part))
    return "\n".join(parts)


def _tool_name(tool: Mapping[str, Any]) -> str:
    fn = tool.get("function")
    return str(fn.get("name", "")) if isinstance(fn, Mapping) else str(tool.get("name", ""))


def _canonical_json(value: Any) -> Any:
    return json.loads(_dumps(value))


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_dumps(value).encode()).hexdigest()
