"""The pre-push hook refuses a push to main before it runs anything slow."""

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HOOK = Path(__file__).resolve().parents[2] / ".githooks" / "pre-push"


def push(refs: str) -> subprocess.CompletedProcess[str]:
    """Run the hook the way git does: one '<local ref> <sha> <remote ref> <sha>' line per ref."""
    return subprocess.run(  # noqa: S603, S607 (our own hook)
        ["bash", str(HOOK)], input=refs, capture_output=True, text=True, timeout=10, check=False
    )


def test_a_push_to_main_is_refused_first() -> None:
    done = push("refs/heads/work abc123 refs/heads/main def456\n")
    assert done.returncode == 1
    assert "refusing to push to main" in done.stderr
    assert "ruff" not in done.stdout  # it stopped before the slow gate started


def test_the_guard_looks_at_every_ref_pushed() -> None:
    done = push("refs/heads/a 1 refs/heads/a 2\nrefs/heads/b 3 refs/heads/main 4\n")
    assert done.returncode == 1 and "refusing to push to main" in done.stderr
