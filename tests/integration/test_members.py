"""Members, roles and invitations (ADR-0010 section 3, SEM-85)."""

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from semula import canonical
from semula.config import Quotas, Settings
from semula.db import Database
from semula.errors import SemulaError
from semula.oidc import Identity
from semula.roles import LEVEL, UNRESTRICTED
from semula.schema import iso
from semula.scope import Principal, Scope
from semula.service import Service
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration


def body(text: str) -> Any:
    return {"messages": [{"role": "user", "content": text}]}


class Team:
    """Workspace ``acme`` with an admin, an editor and a viewer, and workspace ``other``."""

    def __init__(self, tmp_path: Path, **quotas: Any) -> None:
        settings = Settings(data_dir=tmp_path, hosted=True, quotas=Quotas(**quotas))
        self.service = Service(Database(fresh_database_url(tmp_path)), settings)
        self.service.create_workspace("acme")
        self.service.create_workspace("other")
        self.operator = self.service.for_scope(Scope("acme"))
        self.people: dict[str, str] = {}
        for name, role in (("ada", "admin"), ("edd", "editor"), ("vi", "viewer")):
            self.join(name, role)
        self.as_("ada").create_project("shop")

    def person(self, name: str, email: str | None = None) -> str:
        who = Identity("https://idp.test", name, email or f"{name}@example.com", True, name.title())
        found = self.service.sign_in_identity(who)
        self.people[name] = str(found["id"])
        return str(found["id"])

    def join(self, name: str, role: str) -> None:
        person = self.person(name)
        invite = self.operator.create_invitation(f"{name}@example.com", role)
        self.service.accept_invitation(invite["token"], person)

    def as_(self, name: str, workspace: str = "acme") -> Service:
        role = self.service.membership_of(self.people[name], workspace)
        who = Principal(kind="person", id=self.people[name], workspace_id=workspace, role=role)
        return self.service.for_scope(Scope(workspace, who))


@pytest.fixture
def team(tmp_path: Path) -> Team:
    return Team(tmp_path)


def refused(call: Any, status: int, code: str) -> None:
    with pytest.raises(SemulaError) as caught:
        call()
    assert (caught.value.status, caught.value.code) == (status, code), caught.value.message


# --- roles ----------------------------------------------------------------------------------


def test_every_public_method_has_a_role_or_is_listed_as_unrestricted() -> None:
    public = {
        n for n, m in inspect.getmembers(Service, inspect.isfunction) if not n.startswith("_")
    }
    decorated = {n for n in public if hasattr(getattr(Service, n), "needs")}
    assert public == decorated | UNRESTRICTED, sorted(public ^ (decorated | UNRESTRICTED))
    assert decorated & UNRESTRICTED == set(), "a method is both restricted and unrestricted"


def test_a_person_below_the_needed_role_is_refused_before_anything_runs(team: Team) -> None:
    for name in sorted(
        n for n, m in inspect.getmembers(Service, inspect.isfunction) if hasattr(m, "needs")
    ):
        needed = getattr(Service, name).needs
        for who in ("vi", "edd"):
            role = team.service.membership_of(team.people[who], "acme")
            assert role is not None
            if LEVEL[role] >= LEVEL[needed]:
                continue
            method = getattr(team.as_(who), name)
            arity = len(inspect.signature(method).parameters)
            call = (
                (lambda m=method, a=arity: asyncio.run(m(*[None] * a)))
                if inspect.iscoroutinefunction(method)
                else (lambda m=method, a=arity: m(*[None] * a))
            )
            refused(call, 403, "insufficient_role")


def test_a_viewer_reads_and_nothing_more(team: Team) -> None:
    ada, vi = team.as_("ada"), team.as_("vi")
    entry, _ = ada.upsert_entry("shop", body("hello"), {"content": "hi"})
    pending = ada.open_pending(canonical.from_openai(body("waiting")), "shop")
    assert vi.list_entries("shop")["total"] == 1 and vi.get_entry(entry["id"]) and vi.list_pending()
    assert vi.list_projects() and vi.list_calls() == [] and vi.usage()["used"]["projects"] == 2
    refused(lambda: vi.answer_pending(pending, {"content": "x"}), 403, "insufficient_role")
    refused(lambda: vi.update_entry(entry["id"], 1, {"pinned": True}), 403, "insufficient_role")
    refused(
        lambda: vi.test_match(canonical.from_openai(body("x")), "shop"), 403, "insufficient_role"
    )
    refused(lambda: vi.set_mode("record", "shop"), 403, "insufficient_role")


def test_an_editor_does_the_recording_and_review_work_but_not_the_admin_work(team: Team) -> None:
    ada, edd = team.as_("ada"), team.as_("edd")
    entry, _ = ada.upsert_entry("shop", body("hello"), {"content": "hi"})
    pending = ada.open_pending(canonical.from_openai(body("waiting")), "shop")
    edd.answer_pending(pending, {"content": "ok"})
    edd.update_entry(entry["id"], 1, {"response": {"content": "better"}})
    edd.set_mode("record", "shop")
    assert edd.test_match(canonical.from_openai(body("hello")), "shop")["matched"]
    ada.record_candidates(canonical.from_openai(body("hello!")), "shop", [])
    for call in (
        lambda: edd.delete_entry(entry["id"], 2),
        lambda: edd.create_project("mine"),
        lambda: edd.update_project("shop", settings={"tail_n": 2}),
        lambda: edd.delete_project("shop"),
        lambda: edd.import_project(ada.export_project("shop")),
        lambda: edd.export_project("shop"),
        lambda: edd.create_token("t", "record"),
        lambda: edd.list_tokens(),
        lambda: edd.list_audit(),
        lambda: edd.list_members(),
        lambda: edd.create_invitation("x@example.com", "viewer"),
        lambda: edd.delete_workspace("acme"),
        lambda: edd.recount_usage(),
    ):
        refused(call, 403, "insufficient_role")


def test_passing_a_pin_is_for_admins_only(team: Team) -> None:
    ada, edd = team.as_("ada"), team.as_("edd")
    entry, _ = ada.upsert_entry("shop", body("pinned"), {"content": "x"})
    ada.update_entry(entry["id"], 1, {"pinned": True}, actor="human")
    refused(
        lambda: edd.update_entry(entry["id"], 2, {"response": {"content": "y"}}, force=True),
        403,
        "insufficient_role",
    )
    ada.update_entry(entry["id"], 2, {"response": {"content": "y"}}, force=True, actor="human")


def test_an_admin_manages_everything_and_the_actions_are_audited(team: Team) -> None:
    ada = team.as_("ada")
    ada.create_project("another")
    ada.update_project("another", settings={"tail_n": 2})
    ada.create_token("ci", "record")
    assert {m["role"] for m in ada.list_members()} == {"admin", "editor", "viewer"}
    ada.delete_project("another")
    actions = {e["action"] for e in ada.list_audit(limit=100)}
    assert {"project.create", "project.settings", "token.create", "project.delete"} <= actions
    mine = next(e for e in ada.list_audit() if e["action"] == "project.delete")
    assert (mine["principal_kind"], mine["principal_id"]) == ("person", team.people["ada"])


def test_a_person_with_no_membership_can_do_nothing(team: Team) -> None:
    team.person("nobody")
    stranger = team.service.for_scope(
        Scope(
            "acme",
            Principal(kind="person", id=team.people["nobody"], workspace_id="acme", role=None),
        )
    )
    refused(lambda: stranger.list_projects(), 403, "insufficient_role")
    assert team.service.principal_for_session({"person_id": team.people["nobody"]}) is None


# --- members --------------------------------------------------------------------------------


def test_roles_change_and_the_last_admin_cannot_be_demoted_or_leave(team: Team) -> None:
    ada = team.as_("ada")
    refused(lambda: ada.set_member_role(team.people["ada"], "editor"), 409, "last_admin")
    refused(lambda: ada.remove_member(team.people["ada"]), 409, "last_admin")
    ada.set_member_role(team.people["edd"], "admin")
    ada.set_member_role(team.people["ada"], "viewer")  # a second admin now stands in
    refused(lambda: team.as_("edd").remove_member(team.people["edd"]), 409, "last_admin")
    refused(
        lambda: team.as_("edd").set_member_role(team.people["vi"], "chief"), 422, "invalid_role"
    )
    refused(lambda: team.as_("edd").set_member_role("nobody", "viewer"), 404, "member_not_found")
    assert team.service.membership_of(team.people["ada"], "acme") == "viewer"
    assert any(e["action"] == "member.role" for e in team.as_("edd").list_audit())


def test_removing_a_member_closes_their_way_in(team: Team) -> None:
    secret, session = team.service.create_session(team.people["vi"])
    assert team.service.switch_workspace(secret, "acme")
    team.as_("ada").remove_member(team.people["vi"])
    assert team.service.membership_of(team.people["vi"], "acme") is None
    assert team.service.workspaces_of(team.people["vi"]) == []
    fresh = team.service.authenticate_session(secret)
    assert fresh is not None and team.service.principal_for_session(fresh) is None
    assert session["person_id"] == team.people["vi"]


def test_a_person_works_only_in_workspaces_they_belong_to(team: Team) -> None:
    secret, _ = team.service.create_session(team.people["ada"])
    assert team.service.switch_workspace(secret, "acme") is True
    assert team.service.switch_workspace(secret, "other") is False  # not a member
    assert team.service.switch_workspace("not a session", "acme") is False
    session = team.service.authenticate_session(secret)
    assert session is not None
    principal = team.service.principal_for_session(session)
    assert principal is not None and (principal.workspace_id, principal.role) == ("acme", "admin")
    team.service.for_scope(Scope("other")).create_invitation("ada@example.com", "viewer")
    other = team.service.for_scope(Scope("other")).list_invitations()[0]
    assert other["email"] == "ada@example.com"


# --- invitations ----------------------------------------------------------------------------


def test_an_invitation_is_accepted_once_by_the_person_it_names(team: Team) -> None:
    ada = team.as_("ada")
    invite = ada.create_invitation("  New.Person@Example.com ", "editor")
    assert invite["email"] == "new.person@example.com" and invite["token"].startswith("smla_inv_")
    assert (
        ada.list_invitations()[0]["status"] == "open" and "token" not in ada.list_invitations()[0]
    )
    stored = team.service.db.query("SELECT token_hash FROM invitation ORDER BY created_at DESC")[0][
        "token_hash"
    ]
    assert invite["token"] not in stored
    assert team.service.invitation_preview(invite["token"])["role"] == "editor"
    newcomer = team.person("new", "new.person@example.com")
    joined = team.service.accept_invitation(invite["token"], newcomer)
    assert joined == {"workspace_id": "acme", "role": "editor"}
    assert team.service.membership_of(newcomer, "acme") == "editor"
    refused(
        lambda: team.service.accept_invitation(invite["token"], newcomer), 404, "invitation_invalid"
    )
    assert ada.list_invitations()[0]["status"] == "accepted"


def test_an_invitation_is_for_a_verified_matching_email_and_never_a_way_in(team: Team) -> None:
    invite = team.as_("ada").create_invitation("target@example.com", "admin")
    impostor = team.person("impostor", "other@example.com")
    refused(
        lambda: team.service.accept_invitation(invite["token"], impostor),
        403,
        "invitation_email_mismatch",
    )
    assert team.service.membership_of(impostor, "acme") is None
    refused(
        lambda: team.service.accept_invitation("smla_inv_nope", impostor), 404, "invitation_invalid"
    )
    # an unverified email cannot even sign in, so cannot reach an invitation
    refused(
        lambda: team.service.sign_in_identity(
            Identity("https://idp.test", "u", "target@example.com", False, "T")
        ),
        403,
        "email_not_verified",
    )
    # still open for the right person
    right = team.person("target", "target@example.com")
    assert team.service.accept_invitation(invite["token"], right)["role"] == "admin"


def test_an_invitation_expires_after_seven_days_and_can_be_revoked(team: Team) -> None:
    ada = team.as_("ada")
    stale = ada.create_invitation("late@example.com", "viewer")
    gone = ada.create_invitation("gone@example.com", "viewer")
    ada.revoke_invitation(gone["id"])
    person_late = team.person("late", "late@example.com")
    person_gone = team.person("gone", "gone@example.com")
    refused(
        lambda: team.service.accept_invitation(gone["token"], person_gone),
        404,
        "invitation_invalid",
    )
    later = datetime.now(UTC) + timedelta(days=7, seconds=5)
    team.service._now = lambda: later  # type: ignore[method-assign]
    ada = team.as_("ada")  # a scope made after the clock moved
    refused(
        lambda: team.service.accept_invitation(stale["token"], person_late),
        404,
        "invitation_invalid",
    )
    statuses = {i["email"]: i["status"] for i in ada.list_invitations()}
    assert statuses["late@example.com"] == "expired" and statuses["gone@example.com"] == "revoked"
    assert iso(later) > stale["expires_at"]


def test_an_invitation_to_a_deleted_workspace_is_not_valid(team: Team) -> None:
    invite = team.as_("ada").create_invitation("soon@example.com", "viewer")
    person = team.person("soon", "soon@example.com")
    team.operator.delete_workspace("acme")
    refused(
        lambda: team.service.accept_invitation(invite["token"], person), 404, "invitation_invalid"
    )


def test_accepting_again_never_lowers_a_role(team: Team) -> None:
    again = team.operator.create_invitation("edd@example.com", "viewer")
    team.service.accept_invitation(again["token"], team.people["edd"])
    assert team.service.membership_of(team.people["edd"], "acme") == "editor"
    up = team.operator.create_invitation("edd@example.com", "admin")
    team.service.accept_invitation(up["token"], team.people["edd"])
    assert team.service.membership_of(team.people["edd"], "acme") == "admin"


def test_the_members_quota_counts_members_and_open_invitations(tmp_path: Path) -> None:
    team = Team(tmp_path, members=5)  # ada, edd, vi
    ada = team.as_("ada")
    ada.create_invitation("a@example.com", "viewer")
    ada.create_invitation("b@example.com", "viewer")
    refused(lambda: ada.create_invitation("c@example.com", "viewer"), 403, "quota_exceeded")


def test_tokens_cannot_manage_members(team: Team) -> None:
    token = Principal(kind="token", id="T", workspace_id="acme", scope="admin")
    svc = team.service.for_scope(Scope("acme", token))
    for call in (
        lambda: svc.create_invitation("x@example.com", "viewer"),
        lambda: svc.set_member_role(team.people["vi"], "admin"),
        lambda: svc.remove_member(team.people["vi"]),
        lambda: svc.revoke_invitation("x"),
    ):
        refused(call, 403, "forbidden")
