"""``semula entry add``: author a request/response pair from the CLI (SEM-67)."""

import io
import json
import sys
from pathlib import Path

import httpx
import pytest

from semula.cli import main
from tests.support.server import running_server

pytestmark = pytest.mark.integration


def run(url: str, *argv: str) -> int:
    return main(["--url", url, *argv])


def replay(url: str, prompt: str, **extra: object) -> httpx.Response:
    body = {"model": "m", "messages": [{"role": "user", "content": prompt}], **extra}
    return httpx.post(url + "/v1/chat/completions", json=body, timeout=10)


def test_a_new_pair_is_created_and_replays(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path) as url:
        assert run(url, "entry", "add", "default", "--prompt", "Say hi", "--text", "hi!") == 0
        assert "created" in capsys.readouterr().out
        got = replay(url, "Say hi")
        assert got.json()["choices"][0]["message"]["content"] == "hi!"


def test_json_output_is_the_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with running_server(tmp_path) as url:
        assert run(url, "entry", "add", "default", "--prompt", "p", "--text", "a", "--json") == 0
        out = json.loads(capsys.readouterr().out)
        assert out["created"] is True and out["entry"]["version"] == 1


def test_an_existing_entry_exits_4_with_the_current_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path) as url:
        assert run(url, "entry", "add", "default", "--prompt", "p", "--text", "one") == 0
        capsys.readouterr()
        assert run(url, "entry", "add", "default", "--prompt", "p", "--text", "two") == 4
        err = capsys.readouterr().err
        assert "entry_exists" in err and "--version 1" in err
        assert replay(url, "p").json()["choices"][0]["message"]["content"] == "one"
        assert run(url, "entry", "add", "default", "--prompt", "p", "--text", "two", "--json") == 4
        assert json.loads(capsys.readouterr().err)["error"]["current"]["version"] == 1


def test_version_replaces_and_pin_pins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with running_server(tmp_path) as url:
        run(url, "entry", "add", "default", "--prompt", "p", "--text", "one")
        capsys.readouterr()
        args = ("entry", "add", "default", "--prompt", "p", "--text", "two", "--version", "1")
        assert run(url, *args, "--pin", "--json") == 0
        out = json.loads(capsys.readouterr().out)
        assert out["created"] is False and out["entry"]["pinned"] is True
        assert replay(url, "p").json()["choices"][0]["message"]["content"] == "two"


def test_a_request_file_with_tools_and_a_tool_call_answer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    request = {"messages": [{"role": "user", "content": "Weather in KL?"}], "tools": tools}
    answer = {
        "message": {
            "content": None,
            "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "KL"}}}],
        }
    }
    (tmp_path / "req.json").write_text(json.dumps(request))
    (tmp_path / "ans.json").write_text(json.dumps(answer))
    with running_server(tmp_path) as url:
        code = run(
            url, "entry", "add", "default",
            "--request-file", str(tmp_path / "req.json"),
            "--response-file", str(tmp_path / "ans.json"),
        )  # fmt: skip
        assert code == 0
        got = replay(url, "Weather in KL?", tools=tools).json()["choices"][0]["message"]
        assert got["tool_calls"][0]["function"]["name"] == "get_weather"


def test_an_invalid_tool_call_is_rejected_with_exit_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = {"messages": [{"role": "user", "content": "x"}], "tools": []}
    answer = {
        "message": {
            "content": None,
            "tool_calls": [{"function": {"name": "nope", "arguments": {}}}],
        }
    }
    (tmp_path / "req.json").write_text(json.dumps(request))
    (tmp_path / "ans.json").write_text(json.dumps(answer))
    with running_server(tmp_path) as url:
        code = run(
            url, "entry", "add", "default",
            "--request-file", str(tmp_path / "req.json"),
            "--response-file", str(tmp_path / "ans.json"),
        )  # fmt: skip
        assert code == 4


def test_the_answer_can_come_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"content": "from stdin"}'))
    with running_server(tmp_path) as url:
        assert run(url, "entry", "add", "default", "--prompt", "q", "--stdin") == 0
        assert replay(url, "q").json()["choices"][0]["message"]["content"] == "from stdin"


def test_system_and_model_flags_shape_the_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path) as url:
        code = run(
            url, "entry", "add", "default", "--prompt", "q", "--system", "Be brief", "--text", "ok"
        )
        assert code == 0
        body = {
            "model": "m",
            "messages": [
                {"role": "system", "content": "Be brief"},
                {"role": "user", "content": "q"},
            ],
        }
        assert httpx.post(url + "/v1/chat/completions", json=body).status_code == 200
        assert replay(url, "q").status_code != 200  # no system message: a different request


def test_a_prompt_or_request_file_is_required(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path) as url:
        with pytest.raises(SystemExit) as exc:
            run(url, "entry", "add", "default", "--text", "x")
        assert exc.value.code == 2
