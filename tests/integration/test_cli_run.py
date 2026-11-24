"""`semula run -- <command>`: an ephemeral server for one command (SEM-105)."""

import json
import sys
from pathlib import Path

import pytest

from semula.cli import main

pytestmark = pytest.mark.integration

ASK = (
    "import httpx, os, sys\n"
    "base = os.environ['OPENAI_BASE_URL']\n"
    "r = httpx.post(base + '/chat/completions', timeout=30, json={'model': 'semula', "
    "'messages': [{'role': 'user', 'content': sys.argv[1]}]})\n"
    "sys.exit(0 if r.status_code == 200 else 9)\n"
)
SEED = (
    "import httpx, os\n"
    "root = os.environ['SEMULA_URL']\n"
    "r = httpx.post(root + '/api/v1/projects/default/entries', json={'request': {'messages': "
    "[{'role': 'user', 'content': 'hello there'}]}, 'response': {'content': 'hi'}})\n"
    "assert r.status_code == 201, r.text\n"
)


def run(*args: str) -> int:
    return main(["run", *args])


def test_the_command_sees_the_base_url_and_its_exit_code_is_returned(tmp_path: Path) -> None:
    code = "import os, sys; sys.exit(7 if os.environ['OPENAI_BASE_URL'].endswith('/v1') else 1)"
    assert run("--", sys.executable, "-c", code) == 7


def test_replay_reports_a_miss_but_follows_the_command(tmp_path: Path) -> None:
    assert run("--", sys.executable, "-c", ASK, "never recorded") == 9  # replay refuses the miss


def test_strict_fails_on_a_miss_the_command_ignored(tmp_path: Path) -> None:
    ignore = ASK.replace("sys.exit(0 if r.status_code == 200 else 9)", "pass")
    assert run("--", sys.executable, "-c", ignore, "never recorded") == 0
    assert run("--mode", "strict", "--", sys.executable, "-c", ignore, "never recorded") == 1


def test_a_named_store_keeps_recordings_between_runs(tmp_path: Path) -> None:
    store = str(tmp_path / "store")
    assert run("--store", store, "--", sys.executable, "-c", SEED) == 0
    ask = ["--", sys.executable, "-c", ASK, "hello there"]
    assert run("--store", store, "--mode", "strict", *ask) == 0
    assert run("--mode", "strict", *ask) == 9  # a throwaway store starts empty: the miss


def test_the_stats_are_printed_as_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "run", "--", sys.executable, "-c", "pass"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["exit_code"] == 0 and out["check"]["calls"] == 0


def test_a_missing_command_is_a_usage_error(tmp_path: Path) -> None:
    assert run("--") == 2
    assert run("--", "/no/such/command-semula") == 2
