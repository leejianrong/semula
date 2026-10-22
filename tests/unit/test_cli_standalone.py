"""The standalone binary ships the HTTP commands only; the rest must say so, not crash."""

import sys

import pytest

from semula.cli import main

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("argv", "missing", "hint"),
    [
        (["serve"], "uvicorn", "ghcr.io/leejianrong/semula"),
        (["mcp"], "semula.mcp_server", "/mcp/"),
        (["embeddings", "fetch"], "semula.embeddings", "ghcr.io/leejianrong/semula"),
    ],
)
def test_a_command_the_binary_leaves_out_explains_where_to_find_it(
    argv: list[str],
    missing: str,
    hint: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(sys.modules, missing, None)  # importing it now raises ImportError
    assert main(["--url", "http://127.0.0.1:1", *argv]) == 2
    err = capsys.readouterr().err
    assert "not in this build" in err and hint in err
