"""deploy/droplet is the public deployment: what it must never get wrong is checked without Docker
(the whole stack is run by scripts/smoke_droplet.sh in CI). Plain text checks, no YAML library."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

DIR = Path(__file__).resolve().parents[2] / "deploy" / "droplet"
COMPOSE = (DIR / "compose.yaml").read_text()
SERVICES = ("db", "migrate", "semula", "caddy", "purge", "admin")
SECRETS = ("POSTGRES_OWNER_PASSWORD", "SEMULA_DB_PASSWORD", "SEMULA_SECRET", "SEMULA_DOMAIN")


def service(name: str) -> str:
    body = COMPOSE.split("\nservices:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    match = re.search(rf"^  {name}:.*?(?=^  \S|\Z)", body, re.S | re.M)
    assert match, f"no service {name}"
    return match.group(0)


def test_only_caddy_publishes_ports_and_only_80_and_443() -> None:
    for name in SERVICES:
        assert ("ports:" in service(name)) == (name == "caddy"), name
    published = re.findall(r'"\$\{\w+:-(\d+)\}:(\d+)"', service("caddy"))
    assert sorted(container for _, container in published) == ["443", "80"]
    assert sorted(host for host, _ in published) == ["443", "80"]


def test_the_app_connects_as_the_role_that_owns_nothing() -> None:
    url = re.search(r"SEMULA_DATABASE_URL: postgresql://(\w+):", service("semula"))
    assert url and url.group(1) == "semula"
    for name in ("migrate", "purge", "admin"):  # these change the schema or cross tenants
        assert "*owner_url" in service(name), name
    assert "semula_owner" in COMPOSE.split("x-owner-url:")[1].split("x-hardening")[0]


def test_the_app_waits_for_migrations_and_does_not_run_them() -> None:
    app = service("semula")
    assert 'SEMULA_MIGRATE: "off"' in app and 'SEMULA_HOSTED: "on"' in app
    assert re.search(r"migrate:\s*\{condition: service_completed_successfully\}", app)


def test_a_secret_is_required_never_defaulted() -> None:
    for name in SECRETS:
        assert f"${{{name}:?" in COMPOSE, name
        assert f"${{{name}:-" not in COMPOSE and f"${{{name}-" not in COMPOSE, name
    example = (DIR / ".env.example").read_text()
    for name in SECRETS:
        assert re.search(rf"^{name}=", example, re.M), name
    assert not re.search(
        r"^(POSTGRES_OWNER_PASSWORD|SEMULA_DB_PASSWORD|SEMULA_SECRET)=\S", example, re.M
    )


def test_semula_trusts_exactly_one_address_and_it_is_caddys() -> None:
    trusted = re.search(r"SEMULA_TRUSTED_PROXIES: (\S+)", service("semula"))
    caddy = re.search(r"ipv4_address: (\S+)", service("caddy"))
    assert trusted and caddy and trusted.group(1) == caddy.group(1)
    assert "/" not in trusted.group(1) and "*" not in trusted.group(1)
    assert re.search(r"ip_range: 172\.30\.0\.128/25", COMPOSE)  # so nothing else is handed .2


def test_every_container_has_a_memory_limit_and_drops_capabilities() -> None:
    for name in SERVICES:
        block = service(name)
        assert "mem_limit:" in block, name
        assert "*hardening" in block, name


def test_caddy_sets_hsts_and_never_cuts_a_held_call_or_stream() -> None:
    caddyfile = (DIR / "Caddyfile").read_text()
    assert re.search(r'Strict-Transport-Security "max-age=\d{7,}"', caddyfile)
    assert "flush_interval -1" in caddyfile
    assert "response_header_timeout 0" in caddyfile and "read_timeout 0" in caddyfile
    assert "{$SEMULA_DOMAIN}" in caddyfile
    for header in (
        "Permissions-Policy",
        "Cross-Origin-Opener-Policy",
        "Cross-Origin-Resource-Policy",
    ):
        assert header in caddyfile


def test_the_access_log_redacts_the_secrets_that_travel_in_an_address() -> None:
    caddyfile = (DIR / "Caddyfile").read_text()
    assert "format filter" in caddyfile and "request>uri regexp" in caddyfile
    for part in ("/auth/invite/", "code", "state", "sid", "REDACTED"):
        assert part in caddyfile.split("request>uri regexp")[1].splitlines()[0], part


def test_the_database_role_for_the_app_is_not_a_superuser_and_bypasses_nothing() -> None:
    script = (DIR / "initdb" / "01-roles.sh").read_text()
    assert "NOSUPERUSER" in script and "NOBYPASSRLS" in script
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE semula_owner" in script
    assert "OWNER" not in script.replace("semula_owner", "")  # never makes the app role an owner
    assert (DIR / "initdb" / "01-roles.sh").stat().st_mode & 0o111
