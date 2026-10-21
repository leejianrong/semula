import importlib.util
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).parents[2] / "scripts" / "free_port.py"


def load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("free_port", SCRIPT)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_it_skips_a_busy_port() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        taken = busy.getsockname()[1]
        found = load().free_port(taken)
    assert found > taken


def test_it_is_what_make_runs_a_plain_integer_on_stdout() -> None:
    """`make run` once built this with an inline `python -c` whose \\n stayed literal: no port."""
    out = subprocess.run(  # noqa: S603 (our own script)
        [sys.executable, str(SCRIPT), "18000"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip().isdigit() and int(out.stdout) >= 18000


def test_the_makefile_uses_the_script_and_no_inline_python() -> None:
    makefile = (Path(__file__).parents[2] / "Makefile").read_text()
    assert "scripts/free_port.py" in makefile
    assert "python3 -c" not in makefile
