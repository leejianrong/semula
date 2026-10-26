"""The answerer's configuration: an argv list or a URL from the environment, nothing looser."""

import pytest

from semula.config import Settings

pytestmark = pytest.mark.unit


def load(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key in (
        "SEMULA_ANSWERER_COMMAND", "SEMULA_ANSWERER_URL", "SEMULA_ANSWERER_ENV",
        "SEMULA_ANSWERER_BEARER", "SEMULA_ANSWERER_TIMEOUT", "SEMULA_ANSWERER_CONCURRENCY",
    ):  # fmt: skip
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env()


def test_nothing_is_configured_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load(monkeypatch)
    assert not settings.answerer_configured and settings.answerer_timeout == 120.0
    assert settings.answerer_concurrency == 2


def test_a_command_is_a_json_argv_not_a_shell_line(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load(monkeypatch, SEMULA_ANSWERER_COMMAND='["claude", "-p", "Answer; ls $HOME"]')
    assert settings.answerer_command == ("claude", "-p", "Answer; ls $HOME")
    for bad in ("claude -p", "[]", '["a", 3]', '[""]', '{"a": 1}'):
        with pytest.raises(ValueError, match="SEMULA_ANSWERER_COMMAND"):
            load(monkeypatch, SEMULA_ANSWERER_COMMAND=bad)


def test_a_url_must_be_http_and_not_come_with_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load(monkeypatch, SEMULA_ANSWERER_URL="https://hook.example/a").answerer_url
    with pytest.raises(ValueError, match="http"):
        load(monkeypatch, SEMULA_ANSWERER_URL="file:///etc/passwd")
    with pytest.raises(ValueError, match="not both"):
        load(monkeypatch, SEMULA_ANSWERER_URL="http://x/", SEMULA_ANSWERER_COMMAND='["a"]')


def test_our_own_variables_cannot_be_passed_to_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = load(monkeypatch, SEMULA_ANSWERER_ENV="ANTHROPIC_API_KEY, OTHER")
    assert ok.answerer_env == ("ANTHROPIC_API_KEY", "OTHER")
    with pytest.raises(ValueError, match="SEMULA_"):
        load(monkeypatch, SEMULA_ANSWERER_ENV="ANTHROPIC_API_KEY,semula_upstream_key")


def test_the_secrets_stay_out_of_the_settings_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load(
        monkeypatch,
        SEMULA_ANSWERER_COMMAND='["tool", "--token=abc123"]',
        SEMULA_ANSWERER_BEARER="bearer-xyz",
    )
    text = repr(settings)
    assert "abc123" not in text and "bearer-xyz" not in text
    url = load(monkeypatch, SEMULA_ANSWERER_URL="https://h.example/?k=urlsecret")
    assert "urlsecret" not in repr(url)
