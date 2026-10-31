import argparse
from typing import Any

import pytest

from semula import cli

pytestmark = pytest.mark.unit
WARNING = "no SEMULA_API_KEY"
EVERYWHERE = "0.0.0.0"  # noqa: S104  (what the container listens on)


def serve(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], **env: str) -> str:
    for key in ("SEMULA_API_KEY", "SEMULA_HOSTED", "SEMULA_HOST"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    calls: dict[str, Any] = {}
    monkeypatch.setattr("semula.app.create_app", lambda settings: object())
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.update(kw))
    cli._serve(argparse.Namespace(host=None, port=None))
    assert calls["proxy_headers"] is False  # semula decides which proxy to believe
    return capsys.readouterr().err


def test_an_open_server_on_a_public_address_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert WARNING in serve(monkeypatch, capsys, SEMULA_HOST=EVERYWHERE)


def test_a_hosted_server_does_not_warn_because_tokens_protect_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert WARNING not in serve(monkeypatch, capsys, SEMULA_HOST=EVERYWHERE, SEMULA_HOSTED="on")


def test_loopback_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert WARNING not in serve(monkeypatch, capsys)
