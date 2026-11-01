"""Where the CLI keeps the token ``semula login`` earned (SEM-118).

One JSON file, ``credentials.json``, in ``$SEMULA_CONFIG_DIR`` (else ``$XDG_CONFIG_HOME/semula``,
else ``~/.config/semula``): mode 0600 in a 0700 directory, written by replacing the file so a
crash never leaves half of one. Servers are keyed by their address; the last login is the
default server. Standard library only: the standalone binary uses it too.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def directory() -> Path:
    given = os.environ.get("SEMULA_CONFIG_DIR")
    if given:
        return Path(given)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "semula"


def path() -> Path:
    return directory() / "credentials.json"


def _key(url: str) -> str:
    return url.strip().rstrip("/")


def _read() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"servers": {}}
    if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
        return {"servers": {}}
    return data


def _write(data: dict[str, Any]) -> None:
    where = directory()
    where.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        where.chmod(0o700)
    fd, temp = tempfile.mkstemp(dir=where, prefix=".credentials-")  # created 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(temp, path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def save(url: str, entry: dict[str, Any]) -> None:
    """Keep ``entry`` (it holds the token) for the server at ``url`` and make it the default."""
    data = _read()
    data["servers"][_key(url)] = entry
    data["default"] = _key(url)
    _write(data)


def entry_for(url: str) -> dict[str, Any] | None:
    found = _read()["servers"].get(_key(url))
    return found if isinstance(found, dict) else None


def token_for(url: str) -> str | None:
    token = (entry_for(url) or {}).get("token")
    return token if isinstance(token, str) and token else None


def default_url() -> str | None:
    """The server of the last login, if its entry is still there."""
    data = _read()
    chosen = data.get("default")
    return chosen if isinstance(chosen, str) and chosen in data["servers"] else None


def forget(url: str) -> bool:
    """Drop the entry for ``url``; delete the file when nothing is left."""
    data = _read()
    had = data["servers"].pop(_key(url), None) is not None
    if data.get("default") == _key(url):
        data.pop("default", None)
        if data["servers"]:
            data["default"] = next(iter(data["servers"]))
    if not data["servers"]:
        with contextlib.suppress(OSError):
            path().unlink()
    else:
        _write(data)
    return had
