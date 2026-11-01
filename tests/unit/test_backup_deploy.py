"""The backup service in deploy/droplet/backup is wired the way the script and the guide say
(the whole drill is scripts/smoke_backup.sh, in CI). Plain text checks, no YAML library."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "deploy" / "droplet" / "backup"
SCRIPT = (ROOT / "scripts" / "backup.py").read_text()
GUIDE = (ROOT / "docs" / "guide" / "upgrade-and-backup.md").read_text()
OVERLAYS = {"postgres": DIR / "compose.postgres.yaml", "sqlite": DIR / "compose.sqlite.yaml"}
SETTINGS = sorted(set(re.findall(r"^    (SEMULA_[A-Z0-9_]+)", SCRIPT.split('"""')[1], re.M)))


@pytest.mark.parametrize("variant", OVERLAYS)
def test_the_service_is_locked_down_and_publishes_nothing(variant: str) -> None:
    text = OVERLAYS[variant].read_text()
    assert "ports:" not in text and "privileged" not in text
    assert "no-new-privileges:true" in text and "cap_drop: [ALL]" in text
    assert "mem_limit:" in text and "restart: unless-stopped" in text


@pytest.mark.parametrize("variant", OVERLAYS)
def test_it_is_unhealthy_when_a_backup_is_missed(variant: str) -> None:
    text = OVERLAYS[variant].read_text()
    assert '"semula-backup", "check", "--max-hours", "30"' in text
    assert "sleep 300" in text  # a failed backup is retried soon, not tomorrow


@pytest.mark.parametrize("variant", OVERLAYS)
def test_every_setting_the_script_reads_reaches_it(variant: str) -> None:
    text = OVERLAYS[variant].read_text()
    wanted = [s for s in SETTINGS if s.startswith("SEMULA_BACKUP_")]
    assert wanted, "the script documents its settings"
    missing = [s for s in wanted if f"{s}:" not in text]
    assert not missing, missing


def test_postgres_dumps_as_the_owner_not_as_the_app_role() -> None:
    text = OVERLAYS["postgres"].read_text()
    # the app's role is held back by row-level security and would dump no rows
    assert "postgresql://semula_owner:" in text and "postgresql://semula:" not in text
    assert "db: {condition: service_healthy}" in text


def test_sqlite_waits_for_the_database_and_hands_a_restored_file_to_the_server() -> None:
    text = OVERLAYS["sqlite"].read_text()
    assert "migrate: {condition: service_completed_successfully}" in text
    assert 'SEMULA_RESTORE_OWNER: "10001:10001"' in text
    assert "cap_add: [DAC_OVERRIDE, CHOWN]" in text and "user:" not in text


def test_the_image_has_the_script_the_postgres_client_and_openssl() -> None:
    text = (DIR / "Dockerfile").read_text()
    assert "FROM postgres:16" in text and "openssl" in text and "python3" in text
    assert "COPY scripts/backup.py /usr/local/bin/semula-backup" in text


def test_the_guide_documents_every_setting() -> None:
    missing = [s for s in SETTINGS if s not in GUIDE]
    assert not missing, f"docs/guide/upgrade-and-backup.md leaves out {missing}"
