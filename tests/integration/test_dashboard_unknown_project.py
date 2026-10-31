"""An address that names no project is an ordinary thing for a person to type; it must not put an
error, a traceback or the person's text into the server's log (SEM-114: a signed-in person could
otherwise write lines of their choosing, newlines and escape codes included, into the operator's
log by opening a URL)."""

import logging
import time
import urllib.parse
from pathlib import Path

import pytest

from tests.support.server import running_server
from tests.support.ui import Tab

pytestmark = pytest.mark.integration

FORGED = "FORGED-LINE"
HOSTILE = urllib.parse.quote(f"no-such\n{FORGED} \x1b[31m", safe="")


@pytest.mark.parametrize("project", ["no-such", HOSTILE])
def test_an_address_naming_no_project_logs_nothing_and_never_echoes_the_text(
    project: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO), running_server(tmp_path) as url:
        tab = Tab(url, f"/projects/{project}/log")  # a 500 here means the tab never opens
        time.sleep(2.5)  # long enough for every screen's first load, which is where it failed
        tab.close()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage()[:120] for r in errors]
    # uvicorn's own access line shows the address percent-encoded, which cannot start a line
    assert not [line for line in caplog.text.splitlines() if line.startswith(FORGED)]
    assert "\x1b" not in caplog.text
