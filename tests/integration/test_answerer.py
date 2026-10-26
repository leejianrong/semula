"""The answerer hook (SEM-94, ADR-0014): one fresh process per parked call, answered through the
same validation as an agent. A stand-in script plays the command; no real model runs."""

import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support.server import running_server

pytestmark = pytest.mark.integration

STUB = str(Path(__file__).resolve().parents[1] / "support" / "answerer_stub.py")
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "move",
            "parameters": {
                "type": "object",
                "properties": {"dir": {"enum": ["n", "s"]}},
                "required": ["dir"],
            },
        },
    }
]


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "stub.log"
    monkeypatch.setenv("STUB_LOG", str(path))
    monkeypatch.setenv("STUB_PIDFILE", str(tmp_path / "stub.pid"))
    return path


def server(tmp_path: Path, **overrides: Any) -> Any:
    settings: dict[str, Any] = {
        "mode": "record",
        "record_timeout": 20.0,
        "answerer_command": (sys.executable, STUB),
        "answerer_env": ("STUB_LOG", "STUB_PIDFILE"),
        "answerer_timeout": 10.0,
    }
    settings.update(overrides)
    return running_server(tmp_path, **settings)


def enable(url: str, project: str = "default") -> None:
    resp = httpx.patch(f"{url}/api/v1/projects/{project}", json={"settings": {"answerer": True}})
    assert resp.status_code == 200, resp.text


def ask(url: str, text: str, **extra: Any) -> dict[str, Any]:
    """An app's call, made in a thread. ``out['response']`` fills in when it returns."""
    out: dict[str, Any] = {}

    def call() -> None:
        body = {"model": "m", "messages": [{"role": "user", "content": text}], **extra}
        try:
            out["response"] = httpx.post(url + "/v1/chat/completions", json=body, timeout=30)
        except httpx.HTTPError:  # a call left held when its test ends dies with the server
            out["error"] = True

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    out["thread"] = thread
    return out


def reply_of(out: dict[str, Any], wait: float = 15.0) -> dict[str, Any]:
    out["thread"].join(wait)
    assert "response" in out, "the call was never answered"
    assert out["response"].status_code == 200, out["response"].text
    return dict(out["response"].json()["choices"][0]["message"])


def until(check: Callable[[], bool], seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def parked(url: str) -> list[dict[str, Any]]:
    return list(httpx.get(url + "/api/v1/pending").json()["pending"])


def runs(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_each_call_is_answered_by_its_own_process_in_its_own_empty_directory(
    tmp_path: Path, log: Path
) -> None:
    with server(tmp_path) as url:
        enable(url)
        first, second = ask(url, "one"), ask(url, "two")
        a, b = reply_of(first)["content"], reply_of(second)["content"]
        pids = {a.split()[0], b.split()[0]}
        dirs = {a.split("cwd=")[1], b.split("cwd=")[1]}
        assert len(pids) == 2 and len(dirs) == 2  # no shared process, no shared directory
        assert not any(Path(d).exists() for d in dirs)  # and they are cleaned up


def test_a_project_without_the_switch_is_left_for_an_agent(tmp_path: Path, log: Path) -> None:
    with server(tmp_path) as url:
        held = ask(url, "nobody answers this")
        assert until(lambda: bool(parked(url)))
        time.sleep(1.5)
        assert parked(url) and runs(log) == []
        assert held["thread"].is_alive()


def test_a_call_parked_before_the_switch_is_answered_once_it_is_on(
    tmp_path: Path, log: Path
) -> None:
    with server(tmp_path) as url:
        held = ask(url, "waiting")
        assert until(lambda: bool(parked(url)))
        enable(url)
        assert reply_of(held)["content"].startswith("pid=")


def test_a_failing_command_leaves_the_call_parked_and_is_not_retried(
    tmp_path: Path, log: Path
) -> None:
    with server(tmp_path) as url:
        enable(url)
        held = ask(url, "FAIL please")
        assert until(lambda: len(runs(log)) >= 1)
        time.sleep(3.5)  # several look-agains
        assert len(runs(log)) == 1 and held["thread"].is_alive()
        (item,) = parked(url)
        assert item["claimed_until"] is None  # given back for an agent
        answer = httpx.post(
            f"{url}/api/v1/pending/{item['id']}/answer", json={"content": "by hand"}
        )
        assert answer.status_code == 200
        assert reply_of(held)["content"] == "by hand"


@pytest.mark.parametrize("prompt", ["NOTHING", "BADJSON", "BIG"])
def test_empty_malformed_or_oversized_output_leaves_the_call_parked(
    tmp_path: Path, log: Path, prompt: str
) -> None:
    with server(tmp_path) as url:
        enable(url)
        held = ask(url, prompt)
        assert until(lambda: len(runs(log)) >= 1)
        if prompt == "BADJSON":
            assert until(lambda: len(runs(log)) == 2)  # sent back once, with the reason
        time.sleep(2.0)
        assert held["thread"].is_alive() and len(parked(url)) == 1


def test_a_timeout_kills_the_process_and_leaves_the_call_parked(tmp_path: Path, log: Path) -> None:
    pidfile = tmp_path / "stub.pid"
    with server(tmp_path, answerer_timeout=1.0) as url:
        enable(url)
        held = ask(url, "SLEEP forever")
        assert until(pidfile.exists)
        pid = int(pidfile.read_text())
        assert until(lambda: not _alive(pid), 8.0), "the child outlived its timeout"
        assert held["thread"].is_alive() and len(parked(url)) == 1
        assert until(lambda: parked(url)[0]["claimed_until"] is None, 5.0)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is not alive for our purposes
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


def test_the_child_gets_a_stripped_environment(
    tmp_path: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-test-" + "0123456789abcdef" * 2
    monkeypatch.setenv("SEMULA_UPSTREAM_KEY", secret)
    monkeypatch.setenv("SEMULA_API_KEY_LIKE", "x")
    monkeypatch.setenv("SOME_OTHER_SECRET", secret)
    with server(tmp_path) as url:
        enable(url)
        report = json.loads(reply_of(ask(url, "ENV please"))["content"])
        names = report["env"]
        assert not [n for n in names if n.startswith("SEMULA_")]
        assert "SOME_OTHER_SECRET" not in names
        assert "STUB_LOG" in names  # only because the operator named it
        assert report["cwd_empty"] is True
        assert secret not in json.dumps(report)


def test_a_prompt_never_reaches_the_command_line(tmp_path: Path, log: Path) -> None:
    with server(tmp_path, answerer_command=(sys.executable, STUB, "--fixed", "arg")) as url:
        enable(url)
        report = json.loads(reply_of(ask(url, "ARGV; rm -rf / $(whoami) `id`"))["content"])
        assert report["argv"] == ["--fixed", "arg"] and report["prompt_in_argv"] is False


def test_a_rejected_answer_is_sent_back_once_with_the_reason(tmp_path: Path, log: Path) -> None:
    with server(tmp_path) as url:
        enable(url)
        message = reply_of(ask(url, "TOOL please", tools=TOOLS))
        assert message["tool_calls"][0]["function"]["name"] == "move"
        lines = runs(log)
        assert [line.split()[1] for line in lines] == ["attempt=1", "attempt=2"]


def test_a_second_rejection_leaves_the_call_parked(tmp_path: Path, log: Path) -> None:
    with server(tmp_path) as url:
        enable(url)
        held = ask(url, "STUBBORN please", tools=TOOLS)
        assert until(lambda: len(runs(log)) == 2)
        time.sleep(2.5)
        assert len(runs(log)) == 2 and held["thread"].is_alive() and len(parked(url)) == 1


def test_a_tool_call_request_is_answered_from_the_skeleton(tmp_path: Path, log: Path) -> None:
    with server(tmp_path) as url:
        enable(url)
        message = reply_of(ask(url, "SKELETON now", tools=TOOLS))
        assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"dir": "n"}


def test_a_fenced_json_reply_and_plain_text_are_both_accepted(tmp_path: Path, log: Path) -> None:
    with server(tmp_path) as url:
        enable(url)
        assert reply_of(ask(url, "FENCED"))["content"] == "from a fence"
        assert reply_of(ask(url, "PLAIN"))["content"] == "just words"


def test_the_hook_holds_a_claim_while_it_works(tmp_path: Path, log: Path) -> None:
    with server(tmp_path, answerer_timeout=3.0) as url:
        enable(url)
        ask(url, "SLEEP while claimed")
        assert until(lambda: bool(parked(url)) and parked(url)[0]["claimed_until"] is not None)
        taken = httpx.post(url + "/api/v1/pending/claim", json={}).json()
        assert taken == {"pending": None}  # an agent's claim_next skips it meanwhile
        assert until(lambda: parked(url)[0]["claimed_until"] is None, 8.0)
        again = httpx.post(url + "/api/v1/pending/claim", json={}).json()["pending"]
        assert again is not None  # and gets it once the hook has given it back


def test_the_switch_needs_a_configured_command_and_never_works_hosted(tmp_path: Path) -> None:
    with running_server(tmp_path, mode="record") as url:
        resp = httpx.patch(url + "/api/v1/projects/default", json={"settings": {"answerer": True}})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "answerer_not_configured"


def test_enabling_is_audited_and_never_exported_and_the_command_is_never_shown(
    tmp_path: Path, log: Path
) -> None:
    with server(tmp_path) as url:
        enable(url)
        audit = httpx.get(url + "/api/v1/audit").json()
        assert "project.answerer" in json.dumps(audit)
        bundle = httpx.get(url + "/api/v1/projects/default/export").json()
        assert "answerer" not in json.dumps(bundle)
        for path in ("/api/v1/projects/default", "/api/v1/mode", "/health", "/api/v1/audit"):
            assert STUB not in httpx.get(url + path).text


class _Hook(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []
    mode = "ok"

    def log_message(self, *_: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Hook.seen.append(
            {"auth": self.headers.get("Authorization"), "body": body, "path": self.path}
        )
        if _Hook.mode == "redirect" and self.path == "/answer":
            self.send_response(307)  # a redirect that would answer if it were followed
            self.send_header("Location", "/elsewhere")
            self.end_headers()
            return
        payload = b"x" * 2_000_000 if _Hook.mode == "big" else b'{"content": "from the hook"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def hook() -> Iterator[str]:
    _Hook.seen, _Hook.mode = [], "ok"
    httpd = HTTPServer(("127.0.0.1", 0), _Hook)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/answer"
    httpd.shutdown()


def test_a_webhook_gets_the_request_with_the_bearer_and_its_reply_is_the_answer(
    tmp_path: Path, hook: str
) -> None:
    with running_server(
        tmp_path,
        mode="record",
        record_timeout=20.0,
        answerer_url=hook,
        answerer_bearer="hook-secret",
    ) as url:
        enable(url)
        assert reply_of(ask(url, "ask the hook"))["content"] == "from the hook"
        seen = _Hook.seen[0]
        assert seen["auth"] == "Bearer hook-secret"
        assert seen["body"]["request"]["messages"][-1]["content"] == "ask the hook"
        assert seen["body"]["attempt"] == 1


@pytest.mark.parametrize("mode", ["redirect", "big"])
def test_a_webhook_that_redirects_or_overflows_leaves_the_call_parked(
    tmp_path: Path, hook: str, mode: str
) -> None:
    _Hook.mode = mode
    with running_server(tmp_path, mode="record", record_timeout=20.0, answerer_url=hook) as url:
        enable(url)
        held = ask(url, "ask the hook")
        assert until(lambda: len(_Hook.seen) >= 1)
        time.sleep(2.0)
        assert held["thread"].is_alive() and len(parked(url)) == 1
        assert [s["path"] for s in _Hook.seen] == ["/answer"]  # the redirect was not followed


def test_stop_signals_are_not_needed_to_exit(tmp_path: Path, log: Path) -> None:
    # the server shuts down with a call mid-flight without hanging or leaving the child behind
    pidfile = tmp_path / "stub.pid"
    with server(tmp_path) as url:
        enable(url)
        ask(url, "SLEEP at shutdown")
        assert until(pidfile.exists)
        pid = int(pidfile.read_text())
    assert until(lambda: not _alive(pid), 8.0)


def test_a_timeout_kills_the_whole_process_group(tmp_path: Path, log: Path) -> None:
    pidfile = tmp_path / "stub.pid"
    with server(tmp_path, answerer_timeout=1.0) as url:
        enable(url)
        ask(url, "FORK a grandchild")
        assert until(lambda: pidfile.exists() and len(pidfile.read_text().split()) == 2)
        parent, grandchild = (int(p) for p in pidfile.read_text().split())
        assert until(lambda: not _alive(parent) and not _alive(grandchild), 8.0)


def test_the_hosted_server_refuses_the_switch_whatever_the_role(tmp_path: Path) -> None:
    from semula.config import Settings
    from semula.db import Database
    from semula.errors import SemulaError
    from semula.scope import Scope
    from semula.service import Service
    from tests.support.db import fresh_database_url

    settings = Settings(data_dir=tmp_path, hosted=True, answerer_command=(sys.executable, STUB))
    service = Service(Database(fresh_database_url(tmp_path)), settings)
    service.create_workspace("a")
    with pytest.raises(SemulaError) as caught:
        service.for_scope(Scope("a")).update_project("default", settings={"answerer": True})
    assert caught.value.status == 403 and caught.value.param == "settings.answerer"
