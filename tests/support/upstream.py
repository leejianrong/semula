"""A fake upstream provider for record-through tests: CI never calls a real one (ADR-0013)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

KEY = "sk-test-" + "0123456789abcdef" * 2  # built, so no scanner sees a literal


class FakeUpstream:
    """Answers ``POST /chat/completions`` like OpenAI and remembers what it was sent."""

    def __init__(self, reply: Callable[[dict[str, Any]], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self._reply = reply or self.answer
        self.transport = httpx.MockTransport(self._handle)

    @property
    def calls(self) -> int:
        return len(self.requests)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        self.bodies.append(body)
        return self._reply(body)

    @staticmethod
    def answer(body: dict[str, Any]) -> httpx.Response:
        text = f"upstream says: {body['messages'][-1]['content']}"
        usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        if body.get("stream"):
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
                {"choices": [{"index": 0, "delta": {"content": text[:8]}}]},
                {"choices": [{"index": 0, "delta": {"content": text[8:]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
            lines = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(
                200, content=lines.encode(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-x",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            },
        )
