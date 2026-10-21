"""`make demo` starts, seeds and shows something worth looking at (scripts/demo.py)."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e  # two real servers, about 8 s each: not for the fast layer

SCRIPT = Path(__file__).parents[2] / "scripts" / "demo.py"


@pytest.mark.parametrize("flags", [[], ["--record"]])
def test_the_demo_starts_seeds_and_serves_the_dashboard(flags: list[str]) -> None:
    run = subprocess.run(  # noqa: S603 (our own script)
        [sys.executable, str(SCRIPT), "--check", "--port", "18300", *flags],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.startswith("ok: dashboard 200")
