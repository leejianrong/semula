"""The lint gate checks this repo, not the work-in-progress of other checkouts under it."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BAD = "import os\n"  # F401, whatever the file is called


def _ruff(name: str) -> str:
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--force-exclude",
            "--stdin-filename",
            name,
            "-",
        ],
        input=BAD,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    return done.stdout


@pytest.mark.unit
def test_ruff_checks_the_package():
    # the control: the same content is flagged where ruff should look
    assert "F401" in _ruff("src/semula/x.py")


@pytest.mark.unit
@pytest.mark.parametrize("name", [".claude/worktrees/lane/x.py", ".claude/anything.py"])
def test_ruff_skips_agent_worktrees(name):
    assert "F401" not in _ruff(name)
