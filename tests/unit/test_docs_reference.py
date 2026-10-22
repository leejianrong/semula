"""The reference tables in docs/reference/ must say what the code says (SEM-79).

A new command, flag, tool or route that is not in the docs fails here, and so does a row for
something that no longer exists.
"""

import argparse
import asyncio
import re
from pathlib import Path

import pytest

from semula.app import create_app
from semula.cli import build_parser
from semula.config import Settings
from semula.db import Database
from semula.mcp_server import build_mcp
from semula.service import Service

pytestmark = pytest.mark.unit

REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "reference"


def rows(page: str, first_cell: str) -> list[list[str]]:
    """The cells of every table row on ``page`` whose first cell matches ``first_cell``."""
    out = []
    for line in (REFERENCE / page).read_text().splitlines():
        if line.startswith("|") and re.match(rf"\|\s*{first_cell}", line):
            out.append([c.strip() for c in line.strip().strip("|").split("|")])
    return out


def leaves(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        return [(prefix, parser)]
    found = []
    for name, sub in subs[0].choices.items():
        found += leaves(sub, (*prefix, name))
    return found


def help_for(root: argparse.ArgumentParser, path: tuple[str, ...]) -> str | None:
    parser, text = root, None
    for name in path:
        action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        text = next(a.help for a in action._choices_actions if a.dest == name)
        parser = action.choices[name]
    return text


def test_the_cli_table_lists_every_command_with_its_flags_and_help() -> None:
    root = build_parser()
    documented = {
        tuple(re.findall(r"`semula ([^`]+)`", cells[0])[0].split()): cells
        for cells in rows("cli.md", r"`semula ")
    }
    actual = {path: parser for path, parser in leaves(root)}
    assert set(documented) == set(actual), (
        "docs/reference/cli.md and the CLI list different commands"
    )
    for path, parser in actual.items():
        _, flags_cell, help_cell = documented[path]
        flags = {
            o
            for a in parser._actions
            for o in a.option_strings
            if o.startswith("--") and o not in ("--help", "--json")
        }
        positionals = {
            a.dest
            for a in parser._actions
            if not a.option_strings and not isinstance(a, argparse._SubParsersAction)
        }
        name = "semula " + " ".join(path)
        assert set(re.findall(r"--[a-z][a-z-]*", flags_cell)) == flags, f"flags of {name}"
        assert set(re.findall(r"<([a-z-]+)>", flags_cell)) == positionals, f"arguments of {name}"
        assert help_cell == help_for(root, path), f"what {name} does"


def test_the_mcp_table_lists_every_tool_and_its_arguments(tmp_path: Path) -> None:
    service = Service(Database(tmp_path / "docs.db"), Settings(data_dir=tmp_path))
    tools = {t.name: t for t in asyncio.run(build_mcp(service).list_tools())}
    documented = {cells[0].strip("`"): cells for cells in rows("mcp.md", r"`[a-z_]+`")}
    assert set(documented) == set(tools), "docs/reference/mcp.md and the MCP server differ"
    for name, tool in tools.items():
        required = set(tool.parameters.get("required", []))
        expected = {
            a + ("" if a in required else "?") for a in tool.parameters.get("properties", {})
        }
        shown = set(re.findall(r"`([a-z_]+)`(\??)", documented[name][1]))
        assert {a + q for a, q in shown} == expected, f"arguments of {name}"
        assert documented[name][2], f"{name} has no description"


def test_the_rest_table_lists_every_route(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path))
    actual = {
        f"{method.upper()} {path}"
        for path, ops in app.openapi()["paths"].items()
        for method in ops
        if path.startswith(("/v1", "/api/v1", "/health"))
    }
    documented = {
        cells[0].strip("`") for cells in rows("rest.md", r"`(GET|POST|PUT|PATCH|DELETE) ")
    }
    assert documented == actual, "docs/reference/rest.md and the routes differ"
