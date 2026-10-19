import pytest

from semula.responses import AnswerInvalid, build_completion, parse_answer

pytestmark = pytest.mark.unit


def test_shorthand_content_becomes_an_assistant_message() -> None:
    stored = parse_answer({"content": "hi"})
    assert stored["message"] == {"role": "assistant", "content": "hi"}
    assert stored["finish_reason"] == "stop"


def test_full_chat_completion_body_is_accepted_and_reduced() -> None:
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "yo"},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }
    stored = parse_answer(body)
    assert stored["finish_reason"] == "length"
    assert stored["usage"]["total_tokens"] == 3
    assert "id" not in stored["message"]


def test_tool_calls_get_ids_and_default_finish_reason() -> None:
    stored = parse_answer(
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "KL"}}}],
            }
        }
    )
    call = stored["message"]["tool_calls"][0]
    assert call["id"].startswith("call_") and call["type"] == "function"
    assert call["function"]["arguments"] == '{"city": "KL"}'
    assert stored["finish_reason"] == "tool_calls"


@pytest.mark.parametrize(
    ("payload", "param"),
    [
        ("text", None),
        ({}, None),
        ({"choices": []}, "choices"),
        ({"content": 5}, "message.content"),
        ({"message": {"role": "user", "content": "x"}}, "message.role"),
        ({"message": {"role": "assistant", "content": None}}, "message"),
        ({"content": "x", "finish_reason": "bogus"}, "finish_reason"),
        (
            {"message": {"content": None, "tool_calls": [{"function": {}}]}},
            "message.tool_calls[0].function.name",
        ),
        (
            {
                "message": {
                    "content": None,
                    "tool_calls": [{"function": {"name": "f", "arguments": "{bad"}}],
                }
            },
            "message.tool_calls[0].function.arguments",
        ),
    ],
)
def test_invalid_answers_name_the_problem(payload: object, param: str | None) -> None:
    with pytest.raises(AnswerInvalid) as exc:
        parse_answer(payload)
    assert exc.value.status == 422
    assert exc.value.param == param


def test_completion_gets_a_fresh_identity_and_the_request_model() -> None:
    stored = parse_answer({"content": "x"})
    a, b = build_completion(stored, "m1"), build_completion(stored, "m1")
    assert a["id"] != b["id"] and a["model"] == "m1"
    assert a["usage"]["total_tokens"] == 0
