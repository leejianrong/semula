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


# --- the SQLite variant (SEM-126): the same stack on one small droplet, with no Postgres ----------

LITE = (DIR / "compose.sqlite.yaml").read_text()
LITE_SERVICES = ("migrate", "semula", "caddy", "purge", "admin")


def lite(name: str) -> str:
    body = LITE.split("\nservices:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    match = re.search(rf"^  {name}:.*?(?=^  \S|\Z)", body, re.S | re.M)
    assert match, f"no service {name}"
    return match.group(0)


def env_keys(block: str) -> set[str]:
    env = block.split("environment:", 1)[1]
    return set(re.findall(r"^      (\w+):", env.split("\n    depends_on")[0], re.M))


def test_the_sqlite_variant_has_no_database_server_and_no_postgres_settings() -> None:
    assert "image: postgres" not in LITE and "\n  db:" not in LITE
    assert (
        "postgresql://" not in LITE and "POSTGRES_" not in LITE and "SEMULA_DB_PASSWORD" not in LITE
    )
    assert "SEMULA_DATABASE_URL" not in LITE  # the file in the data volume is the default


def test_the_sqlite_variant_is_one_app_container_on_one_shared_data_volume() -> None:
    assert re.findall(r"^  semula:", LITE, re.M) == ["  semula:"]
    assert "replicas" not in LITE and "scale:" not in LITE
    for name in ("migrate", "semula", "purge", "admin"):
        assert "data:/data" in lite(name) or "*data" in lite(name), name
    assert "\nvolumes:\n  data:\n" in LITE
    assert " - data:/data" not in lite("caddy")  # the proxy never sees the database


def test_the_sqlite_variant_keeps_every_guard_of_the_postgres_one() -> None:
    for name in LITE_SERVICES:
        block = lite(name)
        assert "mem_limit:" in block and "*hardening" in block, name
        assert ("ports:" in block) == (name == "caddy"), name
    published = re.findall(r'"\$\{\w+:-(\d+)\}:(\d+)"', lite("caddy"))
    assert sorted(container for _, container in published) == ["443", "80"]
    trusted = re.search(r"SEMULA_TRUSTED_PROXIES: (\S+)", lite("semula"))
    caddy = re.search(r"ipv4_address: (\S+)", lite("caddy"))
    assert trusted and caddy and trusted.group(1) == caddy.group(1)
    assert "ip_range: 172.30.0.128/25" in LITE
    app = lite("semula")
    assert 'SEMULA_MIGRATE: "off"' in app and 'SEMULA_HOSTED: "on"' in app
    assert re.search(r"migrate:\s*\{condition: service_completed_successfully\}", app)
    for name in SECRETS[2:]:
        assert f"${{{name}:?" in LITE and f"${{{name}:-" not in LITE, name
    assert "./Caddyfile:/etc/caddy/Caddyfile:ro" in lite("caddy")  # the one Caddyfile, shared


def test_the_two_variants_configure_the_app_the_same_way_but_for_the_database() -> None:
    assert env_keys(lite("semula")) == env_keys(service("semula")) - {"SEMULA_DATABASE_URL"}
    for name in ("migrate", "purge", "admin"):
        assert env_keys(lite(name)) == env_keys(service(name)) - {"SEMULA_DATABASE_URL"}, name


def test_the_sqlite_variant_fits_a_one_gigabyte_droplet() -> None:
    # What stays up (migrate runs once and exits; admin only when the operator runs it).
    steady = sum(
        int(re.search(r"mem_limit: (\d+)m", lite(n)).group(1)) for n in ("semula", "caddy", "purge")
    )  # type: ignore[union-attr]
    assert steady <= 850
    assert int(re.search(r"mem_limit: (\d+)m", lite("semula")).group(1)) >= 512  # type: ignore[union-attr]
