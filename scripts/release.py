#!/usr/bin/env python3
"""Checks and notes for a release. Stdlib only, so the release workflow runs it before installing.

    release.py check vX.Y.Z    the tag, pyproject, the lockfile and the CHANGELOG agree
    release.py notes vX.Y.Z    print that version's CHANGELOG section (the GitHub Release body)
    release.py flags vX.Y.Z    print KEY=VALUE lines for the workflow: version, prerelease, latest

The version lives in pyproject.toml; ``semula --version`` reads it back from the installed
package. A release PR changes pyproject.toml, runs ``uv lock`` and dates the CHANGELOG section;
the tag then goes on the merge commit.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG = re.compile(r"v(\d+\.\d+\.\d+)")


class ReleaseError(Exception):
    pass


def version_of(tag: str) -> str:
    found = TAG.fullmatch(tag)
    if not found:
        raise ReleaseError(f"'{tag}' is not a release tag: use vMAJOR.MINOR.PATCH, like v0.1.0")
    return found[1]


def pyproject_version(root: Path = ROOT) -> str:
    return str(tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"])


def locked_version(root: Path = ROOT, name: str = "semula") -> str | None:
    lock = tomllib.loads((root / "uv.lock").read_text())
    return next((p["version"] for p in lock["package"] if p["name"] == name), None)


def changelog_section(version: str, root: Path = ROOT) -> str:
    """The text under ``## [version]``, up to the next version heading."""
    text = (root / "CHANGELOG.md").read_text()
    found = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    if not found or not found[1].strip():
        raise ReleaseError(f"CHANGELOG.md has no filled section '## [{version}]'")
    return found[1].strip() + "\n"


def check(tag: str, root: Path = ROOT) -> str:
    version = version_of(tag)
    problems = []
    if (declared := pyproject_version(root)) != version:
        problems.append(f"pyproject.toml says {declared}, the tag says {version}")
    if (locked := locked_version(root)) != version:
        problems.append(f"uv.lock says {locked}, the tag says {version}: run `uv lock`")
    try:
        changelog_section(version, root)
        text = (root / "CHANGELOG.md").read_text()
        heading = re.search(rf"^## \[{re.escape(version)}\][^\n]*", text, re.M)
        if heading and not re.search(r"\d{4}-\d{2}-\d{2}", heading[0]):
            problems.append(f"the CHANGELOG heading for {version} has no release date")
    except ReleaseError as exc:
        problems.append(str(exc))
    if problems:
        raise ReleaseError("\n".join(problems))
    return version


def flags(tag: str) -> dict[str, str]:
    version = version_of(tag)
    return {
        "version": version,
        # A 0.x release is a pre-release on GitHub (it is not the "latest release" there)...
        "prerelease": str(version.startswith("0.")).lower(),
        # ...but the image still moves ``latest``: people pull it by name.
        "latest": "true",
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("check", "notes", "flags"):
        print(__doc__, file=sys.stderr)
        return 2
    command, tag = argv
    try:
        if command == "check":
            check(tag)
            print(f"ok: {tag} matches pyproject.toml, uv.lock and CHANGELOG.md")
        elif command == "notes":
            sys.stdout.write(changelog_section(version_of(tag)))
        else:
            for key, value in flags(tag).items():
                print(f"{key}={value}")
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
