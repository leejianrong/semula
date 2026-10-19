import re

import pytest

from semula.templating import expand, expand_stored, expand_text

pytestmark = pytest.mark.unit

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"


def test_uuid_differs_on_every_occurrence() -> None:
    out = expand_text("{{uuid}} and {{uuid}}", "m")
    a, b = re.findall(UUID, out)
    assert a != b


def test_now_is_an_iso_utc_timestamp() -> None:
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", expand_text("{{now}}", "m"))


def test_request_model_and_whitespace_inside_braces() -> None:
    assert expand_text("model={{ request.model }}", "gpt-9") == "model=gpt-9"


def test_unknown_templates_are_left_intact() -> None:
    text = "keep {{ user.name }} and {{request.path}} and {not} {{"
    assert expand_text(text, "m") == text


def test_expansion_reaches_nested_message_and_chunks_but_not_usage() -> None:
    stored = {
        "message": {
            "role": "assistant",
            "content": "id {{uuid}}",
            "tool_calls": [{"function": {"name": "f", "arguments": '{"at":"{{now}}"}'}}],
        },
        "chunks": [{"content": "{{request.model}}"}],
        "usage": {"note": "{{uuid}}"},
        "finish_reason": "stop",
    }
    out = expand_stored(stored, "m1")
    assert re.fullmatch(f"id {UUID}", out["message"]["content"])
    assert "{{" not in out["message"]["tool_calls"][0]["function"]["arguments"]
    assert out["chunks"] == [{"content": "m1"}]
    assert out["usage"] == {"note": "{{uuid}}"}
    assert stored["message"]["content"] == "id {{uuid}}"  # the stored copy is untouched


def test_non_strings_pass_through() -> None:
    assert expand({"n": 1, "ok": True, "none": None}, "m") == {"n": 1, "ok": True, "none": None}
