import pytest

from semula.canonical import BUILTIN_REDACTIONS, CanonicalRequest, FingerprintConfig, preview_text

pytestmark = pytest.mark.unit

CONFIG = FingerprintConfig(redactions=BUILTIN_REDACTIONS)
KEY = "sk-" + "ant-api03-AbCdEfGhIjKlMnOpQrStUvWx"  # noqa: S105


def req(*messages: tuple[str, object]) -> CanonicalRequest:
    return CanonicalRequest([{"role": r, "content": c} for r, c in messages])


def test_it_is_the_last_message_with_text() -> None:
    r = req(("system", "Be brief."), ("user", "What is 2+2?"), ("assistant", None))
    assert preview_text(r, CONFIG) == "What is 2+2?"


def test_it_is_empty_when_no_message_has_text() -> None:
    assert preview_text(req(("assistant", None), ("user", "   ")), CONFIG) == ""
    assert preview_text(CanonicalRequest([]), CONFIG) == ""


def test_whitespace_is_collapsed_and_the_text_is_cut_at_120_with_an_ellipsis() -> None:
    long = "word  \n" * 100
    out = preview_text(req(("user", long)), CONFIG)
    assert len(out) == 120 and out.endswith("…") and "\n" not in out and "  " not in out
    assert preview_text(req(("user", "short   text")), CONFIG) == "short text"


def test_a_secret_is_redacted_before_the_cut_so_no_part_of_it_survives() -> None:
    text = "x" * 100 + " " + KEY + " tail"
    out = preview_text(req(("user", text)), CONFIG)
    assert "sk-" not in out and "ant-api03" not in out
    # a secret that would have straddled the 120th character is replaced whole
    text = "y" * 110 + " " + KEY
    assert "sk-" not in preview_text(req(("user", text)), CONFIG)


def test_a_private_key_block_never_leaks_its_body() -> None:
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    out = preview_text(req(("user", "use " + block)), CONFIG)
    assert "MIIE" not in out and "[REDACTED]" in out


def test_project_patterns_apply_and_none_means_nothing_is_hidden() -> None:
    config = FingerprintConfig(redactions=(r"ACME-\d+",))
    assert preview_text(req(("user", "ticket ACME-77")), config) == "ticket [REDACTED]"
    assert preview_text(req(("user", "ticket ACME-77")), None) == "ticket ACME-77"


def test_ids_and_paths_are_shown_as_written_not_scrubbed() -> None:
    out = preview_text(req(("user", "Open /home/me/app.py at 2026-10-19T10:00:00Z")), CONFIG)
    assert out == "Open /home/me/app.py at 2026-10-19T10:00:00Z"
