import json

import pytest

from semula.responses import parse_answer
from semula.streaming import assemble, rechunk, sse_events

pytestmark = pytest.mark.unit


def parse_events(events: list[str]) -> list[dict | str]:
    out: list[dict | str] = []
    for e in events:
        assert e.startswith("data: ") and e.endswith("\n\n")
        payload = e[6:-2]
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


def stored(**kw: object) -> dict:
    return parse_answer({"content": "Hello there, streaming world!", **kw})


def test_text_is_rechunked_by_size_and_ends_with_done() -> None:
    events = parse_events(list(sse_events(stored(), "m", chunk_size=10, include_usage=False)))
    assert events[-1] == "[DONE]"
    chunks = [e for e in events if isinstance(e, dict)]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    pieces = [c["choices"][0]["delta"].get("content", "") for c in chunks[1:-1]]
    assert pieces == ["Hello ther", "e, streami", "ng world!"]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert {c["id"] for c in chunks} == {chunks[0]["id"]}
    assert all(c["object"] == "chat.completion.chunk" and c["model"] == "m" for c in chunks)


def test_usage_chunk_only_when_requested() -> None:
    off = parse_events(list(sse_events(stored(), "m", chunk_size=50, include_usage=False)))
    on = parse_events(list(sse_events(stored(), "m", chunk_size=50, include_usage=True)))
    assert not any(isinstance(e, dict) and e["choices"] == [] for e in off)
    usage = [e for e in on if isinstance(e, dict) and e["choices"] == []]
    assert len(usage) == 1 and usage[0]["usage"]["total_tokens"] == 0


def test_tool_calls_stream_as_indexed_argument_fragments() -> None:
    s = parse_answer(
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "get_weather", "arguments": '{"city":"Kuala Lumpur"}'},
                    },
                    {"id": "call_2", "function": {"name": "get_time", "arguments": "{}"}},
                ],
            }
        }
    )
    deltas = rechunk(s["message"], 8)
    assert deltas[0]["tool_calls"][0] == {
        "index": 0,
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": ""},
    }
    assert assemble(deltas) == s["message"]
    events = parse_events(list(sse_events(s, "m", chunk_size=8, include_usage=False)))
    assert [e for e in events if isinstance(e, dict)][-1]["choices"][0][
        "finish_reason"
    ] == "tool_calls"


def test_chunk_size_below_one_is_clamped() -> None:
    assert len(rechunk({"content": "abc"}, 0)) == 3
