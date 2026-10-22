"""``semula admin``: the operator creates workspaces and mints the first tokens (SEM-83)."""

import json
from pathlib import Path

import pytest

from semula.cli import main
from semula.config import Settings
from semula.db import Database
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = fresh_database_url(tmp_path)
    monkeypatch.setenv("SEMULA_DATABASE_URL", url)
    monkeypatch.setenv("SEMULA_DATA_DIR", str(tmp_path))
    return url


def test_the_operator_makes_a_workspace_and_a_token_that_works(
    env: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["admin", "workspace-create", "acme", "--name", "Acme"]) == 0
    assert main(["admin", "workspace-create", "acme"]) == 4  # already there
    assert "workspace_exists" in capsys.readouterr().err
    assert (
        main(
            [
                "--json",
                "admin",
                "token-create",
                "--workspace",
                "acme",
                "--name",
                "ci",
                "--scope",
                "record",
            ]
        )
        == 0
    )
    made = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert made["token"].startswith("smla_live_") and made["scope"] == "record"

    assert main(["admin", "token-list", "--workspace", "acme"]) == 0
    listing = capsys.readouterr().out
    assert made["id"] in listing and made["token"] not in listing and "never used" in listing

    service = Service(Database(env), Settings(data_dir=tmp_path))
    principal = service.authenticate_token(made["token"])
    assert principal is not None and (principal.workspace_id, principal.scope) == ("acme", "record")


def test_rotate_and_revoke_and_the_errors(env: str, capsys: pytest.CaptureFixture[str]) -> None:
    main(["admin", "workspace-create", "acme"])
    main(
        [
            "--json",
            "admin",
            "token-create",
            "--workspace",
            "acme",
            "--name",
            "t",
            "--scope",
            "admin",
        ]
    )
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (
        main(
            ["--json", "admin", "token-rotate", "--workspace", "acme", first["id"], "--grace", "0"]
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert second["id"] != first["id"]
    assert main(["admin", "token-revoke", "--workspace", "acme", second["id"]]) == 0
    assert main(["admin", "token-revoke", "--workspace", "acme", "NOPE"]) == 4
    assert "token_not_found" in capsys.readouterr().err
    assert (
        main(
            [
                "admin",
                "token-create",
                "--workspace",
                "acme",
                "--name",
                "p",
                "--scope",
                "record",
                "--project",
                "nope",
            ]
        )
        == 4
    )
    assert "project_not_found" in capsys.readouterr().err
    assert Scope("acme")  # the scope type is importable where the CLI uses it


def test_usage_and_recount(env: str, capsys: pytest.CaptureFixture[str]) -> None:
    main(["admin", "workspace-create", "acme"])
    assert main(["--json", "admin", "recount", "--workspace", "acme"]) == 0
    used = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert used["projects"] == 1 and used["entries"] == 0  # the default project


def test_deleting_restoring_and_purging_a_workspace(
    env: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["admin", "workspace-create", "acme"])
    assert main(["admin", "workspace-delete", "acme", "--confirm", "wrong"]) == 4
    assert "confirmation_mismatch" in capsys.readouterr().err
    assert main(["admin", "workspace-delete", "acme", "--confirm", "acme"]) == 0
    assert "can be restored until" in capsys.readouterr().out
    assert main(["admin", "workspace-purge"]) == 0
    assert "purged nothing" in capsys.readouterr().out  # inside its 30 days
    assert main(["admin", "workspace-restore", "acme"]) == 0
    assert "restored" in capsys.readouterr().out
    assert main(["admin", "workspace-restore", "acme"]) == 4  # not deleted


def test_the_operator_invites_the_first_admin_who_then_joins(
    env: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from semula.oidc import Identity

    monkeypatch.setenv("SEMULA_PUBLIC_URL", "https://semula.example.com")
    main(["admin", "workspace-create", "acme"])
    capsys.readouterr()
    assert (
        main(["--json", "admin", "invite", "--workspace", "acme", "--email", "Boss@Example.com"])
        == 0
    )
    made = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert made["role"] == "admin" and made["email"] == "boss@example.com"
    assert made["link"].startswith("https://semula.example.com/auth/invite/smla_inv_")

    service = Service(Database(env), Settings(data_dir=tmp_path))
    person = service.sign_in_identity(
        Identity("https://idp.test", "b1", "boss@example.com", True, "Boss")
    )
    service.accept_invitation(made["link"].rsplit("/", 1)[1], person["id"])
    assert main(["admin", "members", "--workspace", "acme"]) == 0
    listing = capsys.readouterr().out
    assert person["id"] in listing and "admin" in listing and "boss@example.com" in listing
    assert main(["admin", "invite", "--workspace", "acme", "--email", "not-an-email"]) == 4
    assert "invalid_email" in capsys.readouterr().err
