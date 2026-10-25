"""What a request already says about the shape of its answer (SEM-97).

Answers are rejected for a tool name, an enum value or a length the request states. This reads
those facts off the request and builds a skeleton answer that passes ``validate_tool_calls``
whenever the schema allows one. It never writes the content: strings are left empty.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

_DEPTH = 6  # a recursive schema must not make an endless skeleton


def answer_shape(tools: list[dict[str, Any]], params: Mapping[str, Any]) -> dict[str, Any]:
    """The tools the request offers (name, schema, a skeleton call for each), a text skeleton,
    and the limit on free text if the app set one (``max_tokens``)."""
    offered: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool.get("function"), Mapping) else tool
        if not isinstance(fn, Mapping) or not fn.get("name"):
            continue
        schema = fn.get("parameters") or {}
        arguments = sample(schema)
        if not isinstance(arguments, dict):  # a call's arguments are always an object
            arguments = {}
        offered.append(
            {
                "name": str(fn["name"]),
                "description": fn.get("description"),
                "parameters": schema,
                "arguments_valid": _valid(schema, arguments),
                "skeleton": {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": str(fn["name"]), "arguments": arguments}}
                        ]
                    }
                },
            }
        )
    max_tokens = params.get("max_tokens")
    return {
        "tools": offered,
        "text": {"content": ""},
        "max_tokens": max_tokens if isinstance(max_tokens, int) else None,
    }


def sample(schema: Any, depth: int = 0) -> Any:
    """The smallest value a JSON schema accepts, as far as this goes (no $ref, no patterns)."""
    if not isinstance(schema, Mapping) or depth > _DEPTH:
        return None
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list) and schema[key]:
            return sample(schema[key][0], depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind is None:
        kind = "object" if "properties" in schema else None
    if kind == "object":
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        return {name: sample(props.get(name, {}), depth + 1) for name in required}
    if kind == "array":
        count = int(schema.get("minItems") or 0)
        return [sample(schema.get("items", {}), depth + 1) for _ in range(count)]
    if kind == "string":
        return "x" * int(schema.get("minLength") or 0)
    if kind in ("integer", "number"):
        return _number(schema, kind)
    if kind == "boolean":
        return False
    return None


def _number(schema: Mapping[str, Any], kind: str) -> int | float:
    step = 1 if kind == "integer" else 0.5
    low, high = schema.get("minimum"), schema.get("maximum")
    if isinstance(schema.get("exclusiveMinimum"), (int, float)):
        low = schema["exclusiveMinimum"] + step
    if isinstance(low, (int, float)):
        return low if kind == "number" else int(-(-low // 1))
    if isinstance(high, (int, float)) and high < 0:
        return high
    return 0


def _valid(schema: Any, value: Any) -> bool:
    try:
        validator = Draft202012Validator(schema or {})
        json.dumps(value)
    except (SchemaError, TypeError, ValueError):
        return False
    return next(iter(validator.iter_errors(value)), None) is None
