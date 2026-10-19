import pytest

from semula.canonical import (
    BUILTIN_REDACTIONS,
    FingerprintConfig,
    exact_fingerprint,
    from_openai,
    normalised_fingerprint,
    redact_request,
)

pytestmark = pytest.mark.unit

CONFIG = FingerprintConfig(redactions=(*BUILTIN_REDACTIONS, r"ACME-\d+"))


def req(text: str, **extra: object):  # type: ignore[no-untyped-def]
    return from_openai({"messages": [{"role": "user", "content": text}], **extra})


@pytest.mark.parametrize(
    "secret",
    [
        "sk-" + "ant-api03-AbCdEfGhIjKlMnOpQrStUvWx",
        "sk-proj-1234567890abcdefghij",
        "AK" + "IAIOSFODNN7EXAMPLE",
        "gh" + "p_" + "a1B2c3D4e5" * 4,
        "xo" + "xb-123456789012-abcdefghij",
        "Bear" + "er eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\ndef\n-----END RSA PRIVATE KEY-----",
        "ACME-4411",
    ],
)
def test_secrets_are_redacted_from_everything_stored(secret: str) -> None:
    live = from_openai(
        {
            "messages": [
                {"role": "user", "content": f"use {secret} to call the api"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "f", "arguments": f'{{"k": "{secret}"}}'}}
                    ],
                },
            ],
            "tools": [{"type": "function", "function": {"name": "f", "description": secret}}],
        }
    )  # fmt: skip
    stored = redact_request(live, CONFIG).to_json()
    assert secret.split("\n")[0] not in stored
    assert "[REDACTED]" in stored


def test_ordinary_text_is_untouched() -> None:
    text = "Ask the sky about bearer of bad news; skip sk-short and ghp_short."
    assert redact_request(req(text), CONFIG).messages == req(text).messages


def test_redaction_leaves_the_live_requests_fingerprints_in_place() -> None:
    live = req("call with sk-" + "ant-api03-AbCdEfGhIjKlMnOpQrStUvWx and ACME-4411")
    stored = redact_request(live, CONFIG)
    assert stored.messages != live.messages
    # the stored copy and the live request agree under the project's config, so a re-key from
    # storage lands on the same normalised fingerprint the live request is looked up by
    assert normalised_fingerprint(stored, CONFIG) == normalised_fingerprint(live, CONFIG)
    assert exact_fingerprint(live, CONFIG) == exact_fingerprint(live, CONFIG)


def test_a_different_secret_still_matches() -> None:
    a = req("token ACME-1111 please")
    b = req("token ACME-2222 please")
    assert normalised_fingerprint(a, CONFIG) == normalised_fingerprint(b, CONFIG)
    assert normalised_fingerprint(a) != normalised_fingerprint(b)


def test_no_redactions_returns_the_same_request() -> None:
    live = req("ACME-1")
    assert redact_request(live, FingerprintConfig()) is live
