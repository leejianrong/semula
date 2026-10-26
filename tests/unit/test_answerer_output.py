"""What the answerer printed becomes an answer payload, or the reason it cannot."""

import pytest

from semula.answerer import Failed, Rejected, parse_output

pytestmark = pytest.mark.unit


def test_plain_text_is_the_reply() -> None:
    assert parse_output(b"  hello there \n") == {"content": "hello there"}


def test_json_objects_and_arrays_are_the_payload() -> None:
    assert parse_output(b'{"content": "x"}') == {"content": "x"}
    assert parse_output(b'["a", "b"]') == ["a", "b"]


def test_one_code_fence_is_unwrapped() -> None:
    assert parse_output(b'```json\n{"content": "x"}\n```') == {"content": "x"}


def test_empty_output_fails_and_broken_json_is_a_rejection_not_a_reply() -> None:
    with pytest.raises(Failed):
        parse_output(b"  \n")
    with pytest.raises(Rejected) as caught:
        parse_output(b'{"content": ')
    assert caught.value.reason["code"] == "invalid_json"
