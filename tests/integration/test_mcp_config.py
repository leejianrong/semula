"""Connect an agent to the hosted MCP in one command (SEM-119): ``semula mcp-config`` turns the
login ``semula login`` stored into the line an agent client needs, without ever printing the token;
the stdio proxy it names reads the token from the stored login."""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from semula import canonical, credentials
from semula.cli import main
from semula.config import Settings
from semula.db import Database
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url
from tests.support.server import running_server

pytestmark = pytest.mark.integration

MESSAGES = [{"role": "user", "content": "what is two plus two"}]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    where = tmp_path / "config"
    monkeypatch.setenv("SEMULA_CONFIG_DIR", str(where))
    for name in ("SEMULA_API_KEY", "SEMULA_URL"):
        monkeypatch.delenv(name, raising=False)
    return where


def hosted(tmp_path: Path) -> tuple[str, Service, str]:
    """A hosted database with a workspace, a project and a record token bound to it."""
    url = fresh_database_url(tmp_path)
    service = Service(Database(url), Settings(data_dir=tmp_path))
    service.create_workspace("acme")
    acme = service.for_scope(Scope("acme"))
    acme.create_project("shop")
    return url, service, acme.create_token("cli", "record", project="shop")["token"]


def log_in(url: str, token: str, project: str | None = "shop") -> None:
    credentials.save(
        url,
        {
            "token": token,
            "token_id": "t",
            "workspace_id": "acme",
            "scope": "record",
            "project": project,
            "expires_at": None,
        },
    )


def last(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return dict(json.loads(capsys.readouterr().out.strip().splitlines()[-1]))


def add(client: str, url: str) -> list[str]:
    return [client, "mcp", "add", "semula", "--", "semula", "--url", url, "mcp"]


def test_it_prints_the_command_for_the_logged_in_server_and_never_the_token(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db, _, token = hosted(tmp_path)
    with running_server(tmp_path, hosted=True, database_url=db) as url:
        log_in(url, token)
        assert main(["--json", "mcp-config"]) == 0
        out, err = capsys.readouterr()
        shown = json.loads(out.strip().splitlines()[-1])
        assert shown["command"] == add("claude", url)
        assert (shown["workspace"], shown["project"], shown["scope"]) == ("acme", "shop", "record")
        assert token not in out + err

        assert main(["mcp-config"]) == 0
        text = capsys.readouterr().out
        assert f"claude mcp add semula -- semula --url {url} mcp" in text and token not in text


def test_the_http_form_reads_the_token_from_the_environment(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db, _, token = hosted(tmp_path)
    with running_server(tmp_path, hosted=True, database_url=db) as url:
        log_in(url, token)
        assert main(["mcp-config", "--http"]) == 0
        text = capsys.readouterr().out
        assert f"{url}/mcp/" in text and "$SEMULA_API_KEY" in text and token not in text


def test_other_clients(tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db, _, token = hosted(tmp_path)
    with running_server(tmp_path, hosted=True, database_url=db) as url:
        log_in(url, token)
        assert main(["--json", "mcp-config", "--client", "codex"]) == 0
        assert last(capsys)["command"] == add("codex", url)
        assert main(["--json", "mcp-config", "--client", "json"]) == 0
        server = {"command": "semula", "args": ["--url", url, "mcp"]}
        assert last(capsys)["config"] == {"mcpServers": {"semula": server}}


def test_without_a_login_it_says_to_log_in(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db, _, _ = hosted(tmp_path)
    with running_server(tmp_path, hosted=True, database_url=db) as url:
        assert main(["--url", url, "mcp-config"]) == 5
        assert "semula login" in capsys.readouterr().err


def test_a_token_the_server_does_not_accept_is_not_handed_to_an_agent(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db, _, _ = hosted(tmp_path)
    with running_server(tmp_path, hosted=True, database_url=db) as url:
        log_in(url, "smla_live_" + "x" * 32)
        assert main(["mcp-config"]) == 5
        assert "claude mcp add" not in capsys.readouterr().out


def fake_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "argv.txt"
    script = bin_dir / "claude"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return record


def test_install_runs_the_clients_own_command(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, _, token = hosted(tmp_path)
    record = fake_client(tmp_path, monkeypatch)
    with running_server(tmp_path, hosted=True, database_url=db) as url:
        log_in(url, token)
        assert main(["mcp-config", "--install", "claude"]) == 0
        assert record.read_text().split() == add("claude", url)[1:]
        assert token not in record.read_text()


def test_install_says_so_when_the_client_is_not_there(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db, _, token = hosted(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    with running_server(tmp_path, hosted=True, database_url=db) as url:
        log_in(url, token)
        assert main(["mcp-config", "--install", "claude"]) == 2
        assert "not found" in capsys.readouterr().err


def test_a_parked_call_is_answered_over_the_hosted_mcp_and_replays(
    tmp_path: Path, home: Path
) -> None:
    """Through the stdio proxy that ``mcp-config`` names, with only the stored login to go on."""
    db, service, token = hosted(tmp_path)
    acme = service.for_scope(Scope("acme"))
    parked = acme.open_pending(canonical.from_openai({"messages": MESSAGES}), "shop")
    with running_server(tmp_path, hosted=True, database_url=db, mode="replay") as url:
        log_in(url, token)

        async def agent() -> None:
            env = {**os.environ, "SEMULA_CONFIG_DIR": str(home)}
            env.pop("SEMULA_API_KEY", None)
            transport = StdioTransport(
                sys.executable, ["-m", "semula", "--url", url, "mcp"], env=env
            )
            async with Client(transport) as c:
                assert "answer_pending" in {t.name for t in await c.list_tools()}
                waiting = await c.call_tool("list_pending", {})
                assert parked in json.dumps(waiting.data)
                await c.call_tool("answer_pending", {"id": parked, "answer": {"content": "four"}})

        asyncio.run(agent())
        resp = httpx.post(
            f"{url}/v1/projects/shop/chat/completions",
            json={"model": "m", "messages": MESSAGES},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["message"]["content"] == "four"
