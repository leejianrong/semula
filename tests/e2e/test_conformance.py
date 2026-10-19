"""Conformance: real client libraries record through and replay from semula.

Each test makes the call twice. The first is recorded (a stand-in agent answers the
parked request); the second must replay and look identical to the client. A failure
names the client field that did not conform.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

openai = pytest.importorskip("openai")
langchain_openai = pytest.importorskip("langchain_openai")

from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from tests.support.agent import agent_answers  # noqa: E402
from tests.support.server import running_server  # noqa: E402

pytestmark = pytest.mark.e2e

WEATHER_ARGS = {"city": "Kuala Lumpur"}


@pytest.fixture(scope="module")
def url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    data: Path = tmp_path_factory.mktemp("conformance")
    with running_server(data, mode="record", record_timeout=20.0) as server_url:
        yield server_url


def sdk(url: str) -> Any:
    return openai.OpenAI(base_url=url + "/v1", api_key="unused", max_retries=0, timeout=30)


def weather_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }


# --- OpenAI SDK ------------------------------------------------------------------------


def test_openai_text_completion_fields(url: str) -> None:
    messages = [{"role": "user", "content": "Say hello (sdk text)."}]
    with agent_answers(url, {"content": "Hello from semula!"}):
        first = sdk(url).chat.completions.create(model="gpt-4o-mini", messages=messages)
    second = sdk(url).chat.completions.create(model="gpt-4o", messages=messages)
    for resp, model in ((first, "gpt-4o-mini"), (second, "gpt-4o")):
        assert resp.object == "chat.completion", "object"
        assert resp.id.startswith("chatcmpl-"), "id"
        assert resp.model == model, "model must echo the request"
        assert resp.choices[0].message.content == "Hello from semula!", "message.content"
        assert resp.choices[0].message.role == "assistant", "message.role"
        assert resp.choices[0].finish_reason == "stop", "finish_reason"
        assert resp.usage is not None and resp.usage.total_tokens == 0, "usage"
    assert first.id != second.id


def test_openai_streaming_text_and_usage_chunk(url: str) -> None:
    messages = [{"role": "user", "content": "Stream a sentence (sdk stream)."}]
    text = "Streaming through the OpenAI SDK works."
    with agent_answers(url, {"content": text}):
        sdk(url).chat.completions.create(model="m", messages=messages)

    stream = sdk(url).chat.completions.create(
        model="m", messages=messages, stream=True, stream_options={"include_usage": True}
    )
    chunks = list(stream)
    assert chunks[0].choices[0].delta.role == "assistant", "first chunk delta.role"
    streamed = "".join((c.choices[0].delta.content or "") for c in chunks if c.choices)
    assert streamed == text, "concatenated delta.content"
    finish = [
        c.choices[0].finish_reason for c in chunks if c.choices and c.choices[0].finish_reason
    ]
    assert finish == ["stop"], "exactly one finish_reason"
    assert chunks[-1].choices == [] and chunks[-1].usage is not None, "trailing usage chunk"
    assert len({c.id for c in chunks}) == 1, "all chunks share one id"


def test_openai_tool_call_non_stream_and_stream(url: str) -> None:
    messages = [{"role": "user", "content": "Weather in KL? (sdk tools)"}]
    answer = {
        "message": {
            "content": None,
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": json.dumps(WEATHER_ARGS)}}
            ],
        }
    }
    with agent_answers(url, answer):
        first = sdk(url).chat.completions.create(
            model="m", messages=messages, tools=[weather_tool()]
        )
    assert first.choices[0].finish_reason == "tool_calls", "finish_reason"
    call = first.choices[0].message.tool_calls[0]
    assert call.type == "function" and call.function.name == "get_weather", "tool_calls[0]"
    assert json.loads(call.function.arguments) == WEATHER_ARGS, "function.arguments"

    names, args, ids = "", "", set()
    for chunk in sdk(url).chat.completions.create(
        model="m", messages=messages, tools=[weather_tool()], stream=True
    ):
        for tc in (chunk.choices[0].delta.tool_calls or []) if chunk.choices else []:
            names += tc.function.name or ""
            args += tc.function.arguments or ""
            if tc.id:
                ids.add(tc.id)
    assert names == "get_weather", "streamed tool name"
    assert json.loads(args) == WEATHER_ARGS, "streamed tool arguments"
    assert ids == {call.id}, "streamed tool call id matches"


def test_openai_models_and_errors(url: str) -> None:
    client = sdk(url)
    assert [m.id for m in client.models.list().data] == ["semula"], "models.list"
    assert client.models.retrieve("semula").id == "semula", "models.retrieve"

    with pytest.raises(openai.NotFoundError) as missing:
        client.models.retrieve("nope")
    assert missing.value.code == "model_not_found", "error.code on 404"

    with pytest.raises(openai.BadRequestError) as bad:
        client.post("/chat/completions", body={"model": "m"}, cast_to=object)
    assert bad.value.status_code == 400, "400 on a malformed body"


async def test_openai_async_client_streams(url: str) -> None:
    messages = [{"role": "user", "content": "Async please (sdk async)."}]
    with agent_answers(url, {"content": "async ok"}):
        async_client = openai.AsyncOpenAI(
            base_url=url + "/v1", api_key="unused", max_retries=0, timeout=30
        )
        # The agent thread answers the request this call parks.
        await async_client.chat.completions.create(model="m", messages=messages)
    stream = await async_client.chat.completions.create(model="m", messages=messages, stream=True)
    text = "".join([(c.choices[0].delta.content or "") async for c in stream if c.choices])
    assert text == "async ok"


def test_replay_miss_is_a_typed_sdk_error_in_replay_mode(tmp_path: Path) -> None:
    with (
        running_server(tmp_path, mode="replay") as replay_url,
        pytest.raises(openai.APIStatusError) as err,
    ):
        sdk(replay_url).chat.completions.create(
            model="m", messages=[{"role": "user", "content": "never recorded"}]
        )
    assert err.value.status_code == 424
    assert err.value.code == "semula_no_match"


# --- LangChain -------------------------------------------------------------------------


def chat_model(url: str, **kw: Any) -> Any:
    return langchain_openai.ChatOpenAI(
        base_url=url + "/v1", api_key="unused", model="gpt-4o-mini", max_retries=0, **kw
    )


def test_langchain_invoke_and_stream(url: str) -> None:
    msg = [HumanMessage(content="Say hi through langchain.")]
    with agent_answers(url, {"content": "Hi from LangChain land."}):
        first = chat_model(url).invoke(msg)
    assert first.content == "Hi from LangChain land.", "invoke content"
    again = chat_model(url).invoke(msg)
    assert again.content == first.content and again.id != first.id

    pieces = [c.content for c in chat_model(url).stream(msg)]
    assert "".join(str(p) for p in pieces) == "Hi from LangChain land.", "stream content"


def test_langchain_tool_calling(url: str) -> None:
    @tool
    def get_weather(city: str) -> str:
        """Get the weather for a city."""
        return "sunny"

    model = chat_model(url).bind_tools([get_weather])
    msg = [HumanMessage(content="Weather in KL? (langchain tools)")]
    answer = {
        "message": {
            "content": None,
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": json.dumps(WEATHER_ARGS)}}
            ],
        }
    }
    with agent_answers(url, answer):
        first = model.invoke(msg)
    replayed = model.invoke(msg)
    for ai in (first, replayed):
        assert ai.tool_calls[0]["name"] == "get_weather", "tool_calls[0].name"
        assert ai.tool_calls[0]["args"] == WEATHER_ARGS, "tool_calls[0].args"


def test_admin_is_unaffected_by_clients(url: str) -> None:
    assert httpx.get(url + "/health").json()["status"] == "ok"
