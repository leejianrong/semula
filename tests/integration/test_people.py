"""People and their sessions at the service layer (ADR-0010 section 1, SEM-84)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from semula.config import Settings
from semula.db import Database
from semula.errors import SemulaError
from semula.oidc import Identity
from semula.schema import iso
from semula.service import IDLE_SESSION_SECONDS, SESSION_SECONDS, Service
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration

GOOGLE = "https://accounts.google.com"
GITHUB = "https://github.com"


@pytest.fixture
def service(tmp_path: Path) -> Service:
    return Service(Database(fresh_database_url(tmp_path)), Settings(data_dir=tmp_path))


def who(
    subject: str = "g-1",
    email: str = "ada@example.com",
    issuer: str = GOOGLE,
    verified: bool = True,
) -> Identity:
    return Identity(issuer=issuer, subject=subject, email=email, verified=verified, name="Ada")


def test_a_first_sign_in_makes_a_person_and_a_later_one_finds_them(service: Service) -> None:
    first = service.sign_in_identity(who())
    again = service.sign_in_identity(who(email="ada@new.example"))
    assert first["id"] == again["id"] and first["created"] is True and again["created"] is False
    assert again["email"] == "ada@example.com"  # the person keeps their first address ...
    row = service.db.query("SELECT email FROM identity")[0]
    assert row["email"] == "ada@new.example"  # ... the identity follows the provider's latest
    assert service.db.query("SELECT last_login_at FROM person")[0]["last_login_at"] is not None


def test_two_providers_never_merge_by_email(service: Service) -> None:
    google = service.sign_in_identity(who(issuer=GOOGLE))
    github = service.sign_in_identity(who(subject="42", issuer=GITHUB))
    assert google["id"] != github["id"]  # same email, two people: linking is a signed-in act


def test_an_unverified_email_is_not_a_sign_in(service: Service) -> None:
    with pytest.raises(SemulaError) as caught:
        service.sign_in_identity(who(verified=False))
    assert (caught.value.status, caught.value.code) == (403, "email_not_verified")
    assert service.db.query("SELECT * FROM person") == []
    with pytest.raises(SemulaError):
        service.sign_in_identity(who(email=""))


def test_a_session_is_found_by_its_secret_and_only_a_hash_is_stored(service: Service) -> None:
    person = service.sign_in_identity(who())
    secret, session = service.create_session(person["id"], client="203.0.113.5")
    assert len(secret) >= 40 and session["csrf"] and session["person_id"] == person["id"]
    stored = service.db.query("SELECT id FROM person_session")[0]["id"]
    assert stored != secret and secret not in stored
    found = service.authenticate_session(secret)
    assert found is not None and found["person_id"] == person["id"]
    assert found["email"] == "ada@example.com"
    assert service.authenticate_session("not a session") is None
    assert service.authenticate_session("") is None


def test_a_session_ends_when_asked_and_on_both_clocks(service: Service) -> None:
    person = service.sign_in_identity(who())
    secret, _ = service.create_session(person["id"])
    service.end_session(secret)
    assert service.authenticate_session(secret) is None

    idle, _ = service.create_session(person["id"])
    old = iso(datetime.now(UTC) - timedelta(seconds=IDLE_SESSION_SECONDS + 5))
    service.db.run("UPDATE person_session SET last_seen_at = :t", {"t": old})
    assert service.authenticate_session(idle) is None, "twelve idle hours"

    absolute, _ = service.create_session(person["id"])
    past = iso(datetime.now(UTC) - timedelta(seconds=5))
    service.db.run(
        "UPDATE person_session SET expires_at = :t WHERE person_id = :p",
        {"t": past, "p": person["id"]},
    )
    assert service.authenticate_session(absolute) is None, "seven days, however active"
    assert service.db.query("SELECT * FROM person_session") == []  # expired rows are removed


def test_signing_in_again_ends_the_session_it_replaces(service: Service) -> None:
    person = service.sign_in_identity(who())
    old, _ = service.create_session(person["id"])
    new, _ = service.create_session(person["id"], replacing=old)  # rotated at sign-in
    assert new != old
    assert service.authenticate_session(old) is None and service.authenticate_session(new)


def test_ending_every_session_of_a_person_and_a_deleted_person(service: Service) -> None:
    person = service.sign_in_identity(who())
    one, _ = service.create_session(person["id"])
    two, _ = service.create_session(person["id"])
    other = service.sign_in_identity(who(subject="g-2", email="grace@example.com"))
    three, _ = service.create_session(other["id"])
    assert service.end_sessions_of(person["id"]) == 2
    assert service.authenticate_session(one) is None and service.authenticate_session(two) is None
    assert service.authenticate_session(three) is not None


def test_last_seen_is_written_at_most_once_a_minute(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    person = service.sign_in_identity(who())
    secret, _ = service.create_session(person["id"])
    first = service.db.query("SELECT last_seen_at FROM person_session")[0]["last_seen_at"]
    service.authenticate_session(secret)
    assert service.db.query("SELECT last_seen_at FROM person_session")[0]["last_seen_at"] == first
    later = datetime.now(UTC) + timedelta(seconds=61)
    monkeypatch.setattr(service, "_now", lambda: later)
    service.authenticate_session(secret)
    assert service.db.query("SELECT last_seen_at FROM person_session")[0]["last_seen_at"] != first


def test_the_lifetimes_are_twelve_hours_idle_and_seven_days() -> None:
    assert IDLE_SESSION_SECONDS == 12 * 3600 and SESSION_SECONDS == 7 * 86400
