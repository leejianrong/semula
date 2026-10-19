from typing import Any

import pytest

from semula.canonical import (
    FingerprintConfig,
    exact_fingerprint,
    from_openai,
    normalised_fingerprint,
    scrub,
)

pytestmark = pytest.mark.unit


def body(content: Any = "hi", **extra: Any) -> dict[str, Any]:
    return {"model": "gpt-x", "messages": [{"role": "user", "content": content}], **extra}


def norm(b: dict[str, Any], cfg: FingerprintConfig | None = None) -> str:
    return normalised_fingerprint(from_openai(b), cfg)


def test_timestamps_do_not_change_the_normalised_fingerprint() -> None:
    a = body("Report at 2026-10-19T10:00:00Z for the team")
    b = body("Report at 2026-10-20 09:15:30+08:00 for the team")
    assert norm(a) == norm(b)
    assert exact_fingerprint(from_openai(a)) != exact_fingerprint(from_openai(b))


def test_uuids_and_absolute_paths_are_scrubbed() -> None:
    a = body("Edit /Users/alex/proj/src/main.py for 3f2504e0-4f89-11d3-9a0c-0305e82c3301")
    b = body("Edit /home/bo/work/src/main.py for 9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d")
    assert norm(a) == norm(b)


def test_windows_paths_are_scrubbed() -> None:
    assert scrub(r"open C:\Users\a\proj\x.py now") == "open [FILE_PATH] now"


def test_reordered_tools_share_a_fingerprint() -> None:
    t1 = {"type": "function", "function": {"name": "alpha", "parameters": {}}}
    t2 = {"type": "function", "function": {"name": "beta", "parameters": {}}}
    assert exact_fingerprint(from_openai(body(tools=[t1, t2]))) == exact_fingerprint(
        from_openai(body(tools=[t2, t1]))
    )


def test_different_messages_differ() -> None:
    assert norm(body("fix auth")) != norm(body("fix billing"))


def test_model_temperature_and_stream_are_ignored() -> None:
    a = body(temperature=0.1, stream=False, max_tokens=10, user="u1", seed=1)
    b = {**body(temperature=0.9, stream=True, max_tokens=999, user="u2", seed=2), "model": "other"}
    assert norm(a) == norm(b)


def test_text_parts_equal_plain_string() -> None:
    parts = [{"type": "text", "text": "hello"}]
    assert norm(body(parts)) == norm(body("hello"))


def test_whitespace_is_collapsed_in_the_normalised_form_only() -> None:
    assert norm(body("a  b\n\nc")) == norm(body("a b c"))
    assert exact_fingerprint(from_openai(body("a  b"))) != exact_fingerprint(
        from_openai(body("a b"))
    )


def test_tail_n_matches_only_the_last_messages() -> None:
    def convo(first: str) -> dict[str, Any]:
        return {
            "messages": [
                {"role": "system", "content": first},
                {"role": "user", "content": "question"},
            ]
        }

    cfg = FingerprintConfig(tail_n=1)
    assert norm(convo("noise one"), cfg) == norm(convo("noise two"), cfg)
    assert norm(convo("noise one")) != norm(convo("noise two"))


def test_extra_scrub_patterns_apply() -> None:
    cfg = FingerprintConfig(extra_scrubs=(r"ticket-\d+",))
    assert norm(body("see ticket-12"), cfg) == norm(body("see ticket-99"), cfg)


def test_round_trip_through_json() -> None:
    from semula.canonical import CanonicalRequest

    req = from_openai(body("x"))
    assert CanonicalRequest.from_json(req.to_json()) == req
