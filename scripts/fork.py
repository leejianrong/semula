#!/usr/bin/env python3
"""Point a fork at its own names: the image registry, the docs site and the repository links.

    fork.py OWNER/REPO            show the files that would change (touches nothing)
    fork.py OWNER/REPO --apply    rewrite them

Rewrites the upstream names in docs, deploy files, the chart, pyproject.toml and zensical.toml:
``ghcr.io/leejianrong/semula``, ``leejianrong.github.io/semula``, ``github.com/leejianrong/semula``
and ``leejianrong/semula``. Leaves history alone (CHANGELOG, ADRs, design notes, the lockfile) and
every other project (``leejianrong/indah``). Safe to run twice. Stdlib only.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_OWNER, UPSTREAM_REPO = "leejianrong", "semula"
SKIP_DIRS = {".git", ".venv", "site", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}
SKIP_FILES = {"CHANGELOG.md", "uv.lock", "fork.py", "test_fork_script.py"}
SKIP_PREFIXES = ("docs/adr/", "docs/design/", "docs/INDAH", "docs/initial-chat")
TEXT_SUFFIXES = {".md", ".toml", ".yaml", ".yml", ".py", ".sh", ".json", ".txt", ".tpl", ".cfg", ""}


class ForkError(Exception):
    pass


def rules(owner: str, repo: str) -> list[tuple[re.Pattern[str], str]]:
    up = re.escape(UPSTREAM_OWNER)
    rp = re.escape(UPSTREAM_REPO)
    return [  # most specific first; each stops short of a longer repo name (semula-x, indah)
        (re.compile(rf"ghcr\.io/{up}/{rp}(?![\w-])"), f"ghcr.io/{owner.lower()}/{repo.lower()}"),
        (re.compile(rf"{up}\.github\.io/{rp}(?![\w-])"), f"{owner.lower()}.github.io/{repo}"),
        (re.compile(rf"github\.com/{up}/{rp}(?![\w-])"), f"github.com/{owner}/{repo}"),
        (re.compile(rf"(?<![\w/.-]){up}/{rp}(?![\w-])"), f"{owner}/{repo}"),
    ]


def candidates(root: Path):  # type: ignore[no-untyped-def]
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or set(rel.parts) & SKIP_DIRS or path.name in SKIP_FILES:
            continue
        if rel.as_posix().startswith(SKIP_PREFIXES):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in {"Dockerfile", "Makefile"}:
            yield path, rel.as_posix()


def run(root: Path, name: str, *, apply: bool) -> list[str]:
    """The files changed (or, without ``apply``, that would be)."""
    parts = name.split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", p) for p in parts):
        raise ForkError(f"{name!r} is not OWNER/REPO, like acme/semula")
    owner, repo = parts
    patterns = rules(owner, repo)
    changed = []
    for path, rel in candidates(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new = text
        for pattern, replacement in patterns:
            new = pattern.sub(replacement, new)
        if new != text:
            changed.append(rel)
            if apply:
                path.write_text(new, encoding="utf-8")
    return changed


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--apply"]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        changed = run(ROOT, args[0], apply="--apply" in argv)
    except ForkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    verb = "rewrote" if "--apply" in argv else "would rewrite"
    for rel in changed:
        print(f"{verb} {rel}")
    print(f"{len(changed)} files" + ("" if "--apply" in argv else " (run again with --apply)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
