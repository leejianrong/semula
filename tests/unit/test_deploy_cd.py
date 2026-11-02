"""Continuous deployment to the droplet: what a bad version or a careless workflow must never do.
The script's real run (pull, up, health, rollback) needs a droplet; these check the guards."""

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "droplet" / "deploy.sh"
DEPLOY = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text()
ACTION = (ROOT / ".github" / "actions" / "deploy" / "action.yml").read_text()


def run(*args: str, stdin_cmd: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin"}
    if stdin_cmd is not None:
        env["SSH_ORIGINAL_COMMAND"] = stdin_cmd  # what sshd hands a forced command
    command = ["bash", str(SCRIPT), *args]  # noqa: S607 (bash, on our own script)
    return subprocess.run(command, capture_output=True, text=True, env=env, timeout=20)  # noqa: S603


def test_the_script_parses_and_stops_on_any_error() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0  # noqa: S603, S607
    assert "set -euo pipefail" in SCRIPT.read_text()


@pytest.mark.parametrize(
    "bad",
    ["", "latest", "0.5", "v0.5.0", "0.5.0; id", "$(id)", "0.5.0 --force", "../0.5.0", "0.5.0\nid"],
)
def test_only_a_plain_version_is_accepted(bad: str) -> None:
    for result in (run(bad), run(stdin_cmd=bad)):
        assert result.returncode == 2, (bad, result.stderr)
        assert "version" in result.stderr


def test_a_forced_command_cannot_pass_anything_but_the_version() -> None:
    result = run(stdin_cmd="0.5.0 && cat /etc/passwd")
    assert result.returncode == 2


def test_both_ways_in_wait_for_the_production_environment() -> None:
    assert "environment: production" in DEPLOY
    assert "workflow_dispatch" in DEPLOY
    assert "environment: production" in RELEASE.split("\n  deploy:\n", 1)[1]


def test_the_deploy_is_not_a_reusable_workflow() -> None:
    # a called workflow cannot see its job's environment secrets: the first run got an empty host
    assert "workflow_call" not in DEPLOY
    assert "uses: ./.github/workflows/deploy.yml" not in RELEASE


def test_the_one_action_does_the_ssh_and_both_workflows_use_it() -> None:
    assert "uses: ./.github/actions/deploy" in DEPLOY
    assert "uses: ./.github/actions/deploy" in RELEASE.split("\n  deploy:\n", 1)[1]


def test_the_action_refuses_empty_settings_before_it_connects() -> None:
    for name in ("host", "host-key", "ssh-key"):
        assert f"{name}:" in ACTION
    assert "is empty" in ACTION


def test_the_action_pins_the_host_key_and_never_trusts_on_first_use() -> None:
    assert "StrictHostKeyChecking=yes" in ACTION
    for text in (ACTION, DEPLOY):
        assert "accept-new" not in text and "StrictHostKeyChecking=no" not in text
        assert "ssh-keyscan" not in text


def test_the_key_is_not_printed_and_the_jobs_cannot_write_to_the_repo() -> None:
    for text in (ACTION, DEPLOY):
        assert "set -x" not in text and 'echo "$DEPLOY_SSH_KEY"' not in text
    assert "contents: read" in DEPLOY
    assert "contents: write" not in DEPLOY


def test_a_release_deploys_only_after_the_image_exists_and_never_on_a_dry_run() -> None:
    block = RELEASE.split("\n  deploy:\n", 1)[1]
    assert "needs: [verify, release, image]" in block
    assert "needs.verify.outputs.publish == 'true'" in block
