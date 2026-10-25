"""The pre-push hook refuses a push to main before it runs anything slow."""

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HOOK = Path(__file__).resolve().parents[2] / ".githooks" / "pre-push"


def push(remote_ref: str) -> subprocess.CompletedProcess[str]:
    line = f"refs/heads/work abc123 {remote_ref} def456\n"
    return subprocess.run(
        ["bash", str(HOOK)], input=line, capture_output=True, text=True, timeout=10
    )


def test_a_push_to_main_is_refused_first() -> None:
    done = push("refs/heads/main")
    assert done.returncode == 1
    assert "refusing to push to main" in done.stderr
    assert "ruff" not in done.stdout  # it stopped before the slow gate started


def test_the_guard_looks_at_every_ref_pushed() -> None:
    line = "refs/heads/a 1 refs/heads/a 2\nrefs/heads/b 3 refs/heads/main 4\n"
    done = subprocess.run(
        ["bash", str(HOOK)], input=line, capture_output=True, text=True, timeout=10
    )
    assert done.returncode == 1 and "refusing to push to main" in done.stderr
