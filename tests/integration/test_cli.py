import json
import threading
from pathlib import Path

import httpx
import pytest

from semula.cli import main
from tests.support.server import running_server

pytestmark = pytest.mark.integration

BODY = {"model": "m", "messages": [{"role": "user", "content": "from the cli test"}]}


def run(url: str, *argv: str) -> int:
    return main(["--url", url, *argv])


def park(url: str) -> tuple[threading.Thread, dict[str, httpx.Response]]:
    result: dict[str, httpx.Response] = {}

    def call() -> None:
        result["resp"] = httpx.post(url + "/v1/chat/completions", json=BODY, timeout=10)

    t = threading.Thread(target=call)
    t.start()
    for _ in range(200):
        if httpx.get(url + "/api/v1/pending").json()["pending"]:
            break
        threading.Event().wait(0.02)
    return t, result


def test_pending_and_answer_release_the_held_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path, mode="record", record_timeout=10.0) as url:
        thread, result = park(url)

        assert run(url, "--json", "pending") == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed[0]["last_message"] == "from the cli test"

        assert run(url, "answer", listed[0]["id"], "--text", "cli says hi") == 0
        thread.join(timeout=10)
        assert result["resp"].json()["choices"][0]["message"]["content"] == "cli says hi"

        assert run(url, "pending") == 0
        assert "no pending requests" in capsys.readouterr().out


def test_exit_codes_for_not_found_rejected_and_unreachable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path, mode="record", record_timeout=10.0) as url:
        assert run(url, "answer", "NOPE", "--text", "x") == 3
        thread, _ = park(url)
        pid = httpx.get(url + "/api/v1/pending").json()["pending"][0]["id"]
        assert run(url, "answer", pid, "--file", str(_write(tmp_path, '{"content": 5}'))) == 4
        assert "message.content" in capsys.readouterr().err
        assert run(url, "answer", pid, "--text", "ok") == 0
        thread.join(timeout=10)
        assert run(url, "answer", pid, "--text", "again") == 4
    assert main(["--url", "http://127.0.0.1:9", "pending"]) == 1


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "answer.json"
    p.write_text(text)
    return p


def test_project_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with running_server(tmp_path) as url:
        assert run(url, "project", "create", "my-app", "--name", "My App") == 0
        assert run(url, "project", "create", "my-app") == 4  # already exists
        assert run(url, "project", "create", "Bad Id") == 4
        capsys.readouterr()
        assert run(url, "--json", "project", "list") == 0
        ids = [p["id"] for p in json.loads(capsys.readouterr().out)]
        assert ids == ["default", "my-app"]
        assert run(url, "project", "delete", "my-app") == 0
        assert run(url, "project", "delete", "my-app") == 3
        assert run(url, "project", "delete", "default") == 4
