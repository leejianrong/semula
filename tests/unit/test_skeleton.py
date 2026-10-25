"""Answer skeletons: what the request says about the answer's shape (SEM-97)."""

from typing import Any

import pytest

from semula.responses import parse_answer, validate_tool_calls
from semula.skeleton import answer_shape, sample

pytestmark = pytest.mark.unit


def tool(name: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    fn: dict[str, Any] = {"name": name, "description": f"does {name}"}
    if parameters is not None:
        fn["parameters"] = parameters
    return {"type": "function", "function": fn}


SCHEMAS = {
    "enum": {
        "type": "object",
        "properties": {"move": {"enum": ["up", "down"]}},
        "required": ["move"],
    },
    "numbers": {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 3},
            "x": {"type": "number", "exclusiveMinimum": 1},
            "neg": {"type": "integer", "maximum": -2},
        },
        "required": ["n", "x", "neg"],
    },
    "strings": {
        "type": "object",
        "properties": {"s": {"type": "string", "minLength": 4, "maxLength": 9}},
        "required": ["s"],
    },
    "nested": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 2,
                "items": {
                    "type": "object",
                    "properties": {"k": {"const": "v"}, "ok": {"type": "boolean"}},
                    "required": ["k", "ok"],
                },
            },
            "opt": {"type": "string"},
        },
        "required": ["items"],
    },
    "union": {
        "type": "object",
        "properties": {"v": {"anyOf": [{"type": "integer", "minimum": 7}, {"type": "null"}]}},
        "required": ["v"],
    },
    "nullable": {
        "type": "object",
        "properties": {"v": {"type": ["null", "string"]}},
        "required": ["v"],
    },
    "none": {},
}


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_skeleton_passes_the_validator_the_answer_goes_through(name: str) -> None:
    shape = answer_shape([tool("act", SCHEMAS[name])], {})
    only = shape["tools"][0]
    assert only["arguments_valid"] is True
    stored = parse_answer(only["skeleton"])  # what answer_pending does with it
    validate_tool_calls(stored, [tool("act", SCHEMAS[name])])


def test_a_tool_request_shows_each_tool_name_schema_and_skeleton() -> None:
    tools = [tool("look"), tool("move", SCHEMAS["enum"])]
    shape = answer_shape(tools, {})
    assert [t["name"] for t in shape["tools"]] == ["look", "move"]
    move = shape["tools"][1]
    assert move["parameters"] == SCHEMAS["enum"] and move["description"] == "does move"
    assert move["skeleton"]["message"]["tool_calls"][0]["function"] == {
        "name": "move",
        "arguments": {"move": "up"},
    }


def test_a_bare_function_entry_is_read_like_a_wrapped_one() -> None:
    shape = answer_shape([{"name": "ping", "parameters": SCHEMAS["none"]}], {})
    assert shape["tools"][0]["name"] == "ping"


def test_the_free_text_limit_is_shown_and_absent_when_unset() -> None:
    assert answer_shape([], {"max_tokens": 60})["max_tokens"] == 60
    assert answer_shape([], {})["max_tokens"] is None
    assert answer_shape([], {})["text"] == {"content": ""}
    assert answer_shape([], {"max_tokens": "lots"})["max_tokens"] is None


def test_an_impossible_schema_is_flagged_not_trusted() -> None:
    impossible = {
        "type": "object",
        "properties": {"s": {"type": "string", "pattern": "^a+$", "minLength": 0}},
        "required": ["s"],
    }
    shape = answer_shape([tool("p", impossible)], {})
    assert shape["tools"][0]["arguments_valid"] is False


def test_a_recursive_schema_ends() -> None:
    node: dict[str, Any] = {"type": "object", "properties": {}, "required": ["next"]}
    node["properties"]["next"] = node
    assert isinstance(sample(node), dict)
