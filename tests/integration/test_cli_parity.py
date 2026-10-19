"""CLI parity with REST/MCP: --json everywhere, stable exit codes, errors on stderr."""

import json
from pathlib import Path

import httpx
import pytest

from semula.cli import build_parser, main
from tests.support.server import running_server

pytestmark = pytest.mark.integration


def run(url: str, *argv: str) -> int:
    return main(["--url", url, *argv])


def out_json(capsys: pytest.CaptureFixture[str]) -> object:
    return json.loads(capsys.readouterr().out)


def seed(url: str, prompt: str = "cli entry", answer: str = "v1") -> dict:
    resp = httpx.post(
        url + "/api/v1/projects/default/entries",
        json={
            "request": {"messages": [{"role": "user", "content": prompt}]},
            "response": {"content": answer},
        },
    )
    assert resp.status_code == 201
    return resp.json()  # type: ignore[no-any-return]


def test_entry_commands_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with running_server(tmp_path) as url:
        entry = seed(url)
        capsys.readouterr()

        assert run(url, "entry", "list", "default", "--json") == 0
        listed = out_json(capsys)
        assert listed["total"] == 1 and listed["entries"][0]["id"] == entry["id"]  # type: ignore[index]

        assert run(url, "entry", "show", entry["id"], "--json") == 0
        assert out_json(capsys)["responses"][0]["message"]["content"] == "v1"  # type: ignore[index]

        assert (
            run(url, "entry", "set", entry["id"], "--version", "1", "--text", "v2", "--json") == 0
        )
        assert out_json(capsys)["version"] == 2  # type: ignore[index]
        assert (
            run(
                url,
                "entry",
                "set",
                entry["id"],
                "--version",
                "2",
                "--text",
                "alt",
                "--add",
                "--selection",
                "cycle",
            )
            == 0
        )
        capsys.readouterr()

        # stale version: rejected (4) with the current entry on stderr as JSON
        assert run(url, "--json", "entry", "set", entry["id"], "--version", "1", "--text", "x") == 4
        err = json.loads(capsys.readouterr().err)["error"]
        assert err["code"] == "version_conflict" and err["current"]["version"] == 3

        assert run(url, "entry", "set", entry["id"], "--version", "3", "--pin", "--as-human") == 0
        assert (
            run(url, "entry", "set", entry["id"], "--version", "4", "--text", "agent") == 4
        )  # pinned
        assert run(url, "entry", "delete", entry["id"], "--version", "4") == 4
        assert run(url, "entry", "delete", entry["id"], "--version", "4", "--as-human") == 0
        assert run(url, "entry", "show", entry["id"]) == 3


def test_mode_and_test_match_and_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path, mode="replay") as url:
        seed(url, "What is the capital of France?", "Paris")
        capsys.readouterr()

        assert run(url, "mode") == 0 and capsys.readouterr().out.strip() == "replay"
        assert run(url, "mode", "record", "--json") == 0
        assert out_json(capsys) == {"mode": "record"}

        assert run(url, "test-match", "--prompt", "What is the capital of France?", "--json") == 0
        hit = out_json(capsys)
        assert hit["matched"] and hit["tier"] == "exact" and hit["would"] == "serve"  # type: ignore[index]

        assert run(url, "test-match", "--prompt", "What is the capital of Spain?") == 0
        human = capsys.readouterr().out
        assert "no match (would park)" in human and "nearest" in human

        body = tmp_path / "req.json"
        body.write_text(
            json.dumps(
                {"messages": [{"role": "user", "content": "What is the capital of France?"}]}
            )
        )
        assert run(url, "test-match", "--file", str(body), "--json") == 0
        assert out_json(capsys)["matched"] is True  # type: ignore[index]

        assert run(url, "project", "settings", "default", "--set", '{"tail_n": 2}', "--json") == 0
        assert out_json(capsys)["tail_n"] == 2  # type: ignore[index]
        assert run(url, "project", "settings", "default", "--set", '{"tail_n": 0}') == 4
        assert run(url, "project", "settings", "default", "--set", "{bad") == 4
        assert run(url, "test-match", "--project", "ghost", "--prompt", "x") == 3


def test_json_flag_works_before_or_after_the_command_and_every_command_has_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_server(tmp_path) as url:
        assert run(url, "--json", "pending") == 0
        before = capsys.readouterr().out
        assert run(url, "pending", "--json") == 0
        assert capsys.readouterr().out == before == "[]\n"

    parser = build_parser()
    commands = [
        ["serve"], ["mcp"], ["pending"], ["show", "x"], ["answer", "x", "--text", "t"],
        ["project", "list"], ["project", "create", "p"], ["project", "delete", "p"],
        ["project", "settings", "p"], ["entry", "list", "p"], ["entry", "show", "e"],
        ["entry", "set", "e", "--version", "1"], ["entry", "delete", "e", "--version", "1"],
        ["mode"], ["test-match", "--prompt", "x"],
    ]  # fmt: skip
    for argv in commands:
        assert parser.parse_args([*argv, "--json"]).json is True, argv
        assert parser.parse_args(argv).json is False, argv


def test_usage_errors_exit_2_and_errors_go_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["entry", "set", "e"])  # --version is required
    assert exc.value.code == 2
    capsys.readouterr()
    with running_server(tmp_path) as url:
        assert run(url, "show", "NOPE") == 3
        captured = capsys.readouterr()
        assert captured.out == "" and "pending_not_found" in captured.err


def test_help_documents_the_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    for code in ("0  ok", "3  not found", "4  rejected", "5  authentication"):
        assert code in text
