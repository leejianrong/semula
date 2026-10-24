"""Forward a miss to a real OpenAI-compatible provider (ADR-0013).

Spends someone's money, so it is built to leak nothing and ask for nothing it need not: the
request that goes out is the app's own body with the server's key, never the app's headers, and
nothing the provider says is passed back until the key has been scrubbed from it.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from .canonical import BUILTIN_REDACTIONS
from .config import Settings
from .errors import SemulaError

T = TypeVar("T")
MAX_ERROR_CHARS = 300


class Upstream:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._url = settings.upstream_url
        self._key = settings.upstream_key
        self._timeout = settings.upstream_timeout
        self._transport = transport
        self._flights: dict[tuple[str, str, str], asyncio.Future[Any]] = {}

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def scrub(self, text: str) -> str:
        """``text`` without the key, and without anything else that looks like a credential."""
        if self._key:
            text = text.replace(self._key, "[REDACTED]")
        for pattern in BUILTIN_REDACTIONS:
            text = re.sub(pattern, "[REDACTED]", text)
        return text

    def _error(self, status: int, code: str, message: str) -> SemulaError:
        return SemulaError(
            status,
            code,
            self.scrub(message)[:MAX_ERROR_CHARS],
            err_type="semula_error",
        )

    async def once(self, key: tuple[str, str, str], work: Callable[[], Awaitable[T]]) -> T:
        """Run ``work`` once for ``key``; a caller that arrives meanwhile awaits its result."""
        flight = self._flights.get(key)
        if flight is not None:
            return await asyncio.shield(flight)
        flight = asyncio.get_running_loop().create_future()
        self._flights[key] = flight
        try:
            result = await work()
        except BaseException as exc:
            flight.set_exception(exc)
            flight.exception()  # mark retrieved: nobody may be waiting
            raise
        else:
            flight.set_result(result)
            return result
        finally:
            self._flights.pop(key, None)

    async def complete(self, body: dict[str, Any], model: str | None) -> dict[str, Any]:
        """Ask the provider; return something ``parse_answer`` takes: a completion body, or
        ``{"chunks": [...]}`` when the app streams. Raises a ``SemulaError`` safe to show."""
        if not self._key:
            raise self._error(503, "semula_upstream_unconfigured", "No upstream key is set.")
        sent = dict(body)
        if model:
            sent["model"] = model
        stream = bool(sent.get("stream"))
        if stream:
            options = sent.get("stream_options")
            sent["stream_options"] = {
                **(options if isinstance(options, dict) else {}),
                "include_usage": True,
            }
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        try:
            async with (
                httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client,
                client.stream(
                    "POST", f"{self._url}/chat/completions", json=sent, headers=headers
                ) as resp,
            ):
                if resp.status_code >= 400:
                    raise self._error(
                        502,
                        "semula_upstream_error",
                        f"The upstream answered {resp.status_code}: "
                        f"{_message_of((await resp.aread()).decode(errors='replace'))}",
                    )
                if not stream:
                    return dict(_json(await resp.aread(), self))
                chunks: list[Any] = []
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunks.append(_json(data.encode(), self))
                return {"chunks": chunks}
        except httpx.TimeoutException as exc:
            raise self._error(
                504, "semula_upstream_timeout", "The upstream did not answer in time."
            ) from exc
        except httpx.HTTPError as exc:
            raise self._error(
                502,
                "semula_upstream_error",
                f"The upstream could not be reached ({type(exc).__name__}).",
            ) from exc


def _json(raw: bytes, upstream: Upstream) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise upstream._error(
            502, "semula_upstream_error", "The upstream answered with something that is not JSON."
        ) from exc


def _message_of(text: str) -> str:
    """The provider's own error message when it sent the usual shape, else its text."""
    try:
        found = json.loads(text)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return text
    return str(found)
