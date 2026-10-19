"""Export bundles to, and import them from, a directory of JSON files (SEM-37).

Layout, built to commit to git and diff::

    <dir>/semula.json            format and project (id, name, settings)
    <dir>/entries/<ulid>.json    one recorded entry
    <dir>/aliases/<ulid>.json    one approved near-miss

Files are written with sorted keys and a trailing newline, so an unchanged project exports
byte-for-byte the same. Pure file handling: the service builds and applies the bundle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FORMAT = "semula/1"
MANIFEST = "semula.json"
KINDS = {"entries": "entries", "aliases": "aliases"}


class BundleError(ValueError):
    """The directory is not a readable semula export."""


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_bundle(bundle: dict[str, Any], directory: Path) -> dict[str, int]:
    """Write a bundle; files for entries or aliases no longer in it are removed."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST).write_text(
        _dump({"format": bundle["format"], "project": bundle["project"]}), encoding="utf-8"
    )
    counts: dict[str, int] = {}
    for kind in KINDS:
        folder = directory / kind
        folder.mkdir(exist_ok=True)
        wanted = {f"{item['id']}.json": item for item in bundle[kind]}
        for stale in folder.glob("*.json"):
            if stale.name not in wanted:
                stale.unlink()
        for name, item in wanted.items():
            (folder / name).write_text(_dump(item), encoding="utf-8")
        counts[kind] = len(wanted)
    return counts


def read_bundle(directory: Path) -> dict[str, Any]:
    manifest = directory / MANIFEST
    if not manifest.is_file():
        raise BundleError(f"{directory} has no {MANIFEST}: it is not a semula export.")
    try:
        bundle: dict[str, Any] = json.loads(manifest.read_text(encoding="utf-8"))
        for kind in KINDS:
            folder = directory / kind
            bundle[kind] = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(folder.glob("*.json"))
            ]
    except (OSError, ValueError) as exc:
        raise BundleError(f"could not read {directory}: {exc}") from exc
    return bundle
