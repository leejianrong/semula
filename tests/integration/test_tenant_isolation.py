"""Workspace A can never see or change workspace B (ADR-0011).

Every public method of the service is called as workspace A with workspace B's ids and
projects, and must answer 404 (as for something that does not exist), or with nothing. A new
public method that is not listed here fails the test that counts them.
"""

import asyncio
import inspect
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from semula import canonical
from semula.config import Settings
from semula.db import Database
from semula.errors import SemulaError
from semula.scope import Scope
from semula.service import Candidate, Service
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration


def req(text: str) -> Any:
    return canonical.from_openai({"messages": [{"role": "user", "content": text}]})


class World:
    """Two workspaces on one database. B has a project with everything in it; A has its own."""

    def __init__(self, tmp_path: Path) -> None:
        self.service = Service(Database(fresh_database_url(tmp_path)), Settings(data_dir=tmp_path))
        self.service.create_workspace("a")
        self.service.create_workspace("b")
        self.a = self.service.for_scope(Scope("a"))
        self.b = self.service.for_scope(Scope("b"))
        self.a.create_project("mine")
        self.a.upsert_entry(
            "mine", {"messages": [{"role": "user", "content": "a's own"}]}, {"content": "a"}
        )
        self.b.create_project("shop")
        body = {"messages": [{"role": "user", "content": "b's secret prompt"}]}
        entry, _ = self.b.upsert_entry("shop", body, {"content": "b's secret answer"})
        self.entry: str = entry["id"]
        self.b.record_candidates(req("b's secret prompts"), "shop", [Candidate(self.entry, 0.8)])
        self.b.record_candidates(req("b's secret prompt too"), "shop", [Candidate(self.entry, 0.7)])
        first, second = self.b.list_candidates("shop")
        self.candidate: str = first["id"]
        self.b.approve_candidate(second["id"])  # makes an alias
        self.pending: str = self.b.open_pending(req("b is waiting"), "shop")
        self.b.log_call(
            "hit", 0.0, project="shop", entry_id=self.entry, req=req("b's secret prompt")
        )
        self.bundle = self.b.export_project("shop")


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    yield World(tmp_path)


def missing(call: Callable[[], Any], code: str | None = None) -> None:
    with pytest.raises(SemulaError) as caught:
        call()
    assert caught.value.status == 404, f"{caught.value.status} {caught.value.code}"
    if code:
        assert caught.value.code == code


def nothing_of_b(value: Any) -> None:
    text = repr(value)
    assert "b's secret" not in text and "shop" not in text and "b is waiting" not in text


# Each scenario takes the world and calls one method as workspace A with B's data.
SCENARIOS: dict[str, Callable[[World], None]] = {
    "answer_pending": lambda w: missing(
        lambda: w.a.answer_pending(w.pending, {"content": "x"}), "pending_not_found"
    ),
    "approve_candidate": lambda w: missing(
        lambda: w.a.approve_candidate(w.candidate), "candidate_not_found"
    ),
    "reject_candidate": lambda w: missing(
        lambda: w.a.reject_candidate(w.candidate), "candidate_not_found"
    ),
    "get_candidate": lambda w: missing(
        lambda: w.a.get_candidate(w.candidate), "candidate_not_found"
    ),
    "delete_entry": lambda w: missing(lambda: w.a.delete_entry(w.entry, 1), "entry_not_found"),
    "update_entry": lambda w: missing(
        lambda: w.a.update_entry(w.entry, 1, {"response": {"content": "x"}}), "entry_not_found"
    ),
    "get_entry": lambda w: missing(lambda: w.a.get_entry(w.entry), "entry_not_found"),
    "entry_history": lambda w: missing(lambda: w.a.entry_history(w.entry), "entry_not_found"),
    "get_pending": lambda w: missing(lambda: w.a.get_pending(w.pending), "pending_not_found"),
    "get_project": lambda w: missing(lambda: w.a.get_project("shop"), "project_not_found"),
    "require_project": lambda w: missing(lambda: w.a.require_project("shop"), "project_not_found"),
    "project_settings": lambda w: missing(
        lambda: w.a.project_settings("shop"), "project_not_found"
    ),
    "mode_of": lambda w: missing(lambda: w.a.mode_of("shop"), "project_not_found"),
    "record_timeout": lambda w: missing(lambda: w.a.record_timeout("shop"), "project_not_found"),
    "list_models": lambda w: missing(lambda: w.a.list_models("shop"), "project_not_found"),
    "through_enabled": lambda w: missing(lambda: w.a.through_enabled("shop"), "project_not_found"),
    "upstream_usage_of": lambda w: missing(
        lambda: w.a.upstream_usage_of("shop"), "project_not_found"
    ),
    "upstream_dry_run_misses": lambda w: missing(
        lambda: w.a.upstream_dry_run_misses("shop"), "project_not_found"
    ),
    "reset_upstream_usage": lambda w: missing(
        lambda: w.a.reset_upstream_usage("shop"), "project_not_found"
    ),
    "forward_miss": lambda w: missing(
        lambda: asyncio.run(w.a.forward_miss(req("x"), {"messages": []}, "shop")),
        "project_not_found",
    ),
    "update_project": lambda w: missing(
        lambda: w.a.update_project("shop", name="mine now"), "project_not_found"
    ),
    "set_mode": lambda w: missing(lambda: w.a.set_mode("record", "shop"), "project_not_found"),
    "delete_project": lambda w: missing(lambda: w.a.delete_project("shop"), "project_not_found"),
    "export_project": lambda w: missing(lambda: w.a.export_project("shop"), "project_not_found"),
    "list_entries": lambda w: missing(lambda: w.a.list_entries("shop"), "project_not_found"),
    "list_pending": lambda w: (
        missing(lambda: w.a.list_pending("pending", "shop"), "project_not_found"),
        nothing_of_b(w.a.list_pending("pending")),
        nothing_of_b(w.a.list_pending(None)),
    ),
    "wait_for_pending": lambda w: (
        missing(lambda: asyncio.run(w.a.wait_for_pending("shop", 0.1)), "project_not_found"),
        nothing_of_b(asyncio.run(w.a.wait_for_pending(None, 0.1))),
    ),
    "wait_for_answer": lambda w: (
        w.b.answer_pending(w.pending, {"content": "b's secret answer"}),
        _no_answer_for_a(w),
    ),
    "open_pending": lambda w: missing(
        lambda: w.a.open_pending(req("x"), "shop"), "project_not_found"
    ),
    "list_candidates": lambda w: (
        missing(lambda: w.a.list_candidates("shop"), "project_not_found"),
        nothing_of_b(w.a.list_candidates()),
        nothing_of_b(w.a.list_candidates(None, None)),
    ),
    "record_candidates": lambda w: missing(
        lambda: w.a.record_candidates(req("x"), "shop", [Candidate(w.entry, 0.9)]),
        "project_not_found",
    ),
    "queue_counts": lambda w: (
        missing(lambda: w.a.queue_counts("shop"), "project_not_found"),
        _equal(w.a.queue_counts(), {"pending": 0, "review": 0}),
    ),
    "list_projects": lambda w: _equal([p["id"] for p in w.a.list_projects()], ["default", "mine"]),
    "list_calls": lambda w: (
        missing(lambda: w.a.list_calls(project="shop"), "project_not_found"),
        nothing_of_b(w.a.list_calls()),
    ),
    "log_stats": lambda w: (
        missing(lambda: w.a.log_stats("shop"), "project_not_found"),
        _equal(w.a.log_stats()["totals"]["calls"], 0),
    ),
    "prune_log": lambda w: (w.a.prune_log(), _equal(len(w.b.list_calls()), 1)),
    "find": lambda w: _equal(w.a.find(req("b's secret prompt"), "mine"), None),
    "lookup": lambda w: _equal(w.a.lookup(req("b's secret prompt"), "mine").match, None),
    "test_match": lambda w: (
        missing(lambda: w.a.test_match(req("b's secret prompt"), "shop"), "project_not_found"),
        _equal(w.a.test_match(req("b's secret prompt"), "mine")["matched"], False),
    ),
    "embeddings_status": lambda w: nothing_of_b(w.a.embeddings_status()),
    "upsert_entry": lambda w: (
        missing(
            lambda: w.a.upsert_entry(
                "shop", {"messages": [{"role": "user", "content": "x"}]}, {"content": "x"}
            ),
            "project_not_found",
        ),
        _same_prompt_is_a_new_entry(w),
    ),
    "create_project": lambda w: (
        w.a.create_project("shop"),  # the slug is free in A: B's project is not A's business
        _equal(w.a.list_entries("shop")["total"], 0),
        _equal(w.b.list_entries("shop")["total"], 1),
    ),
    "import_project": lambda w: _import_into_a(w),
    "create_token": lambda w: missing(
        lambda: w.a.create_token("t", "record", project="shop"), "project_not_found"
    ),
    "list_tokens": lambda w: nothing_of_b(w.a.list_tokens()),
    "list_audit": lambda w: nothing_of_b(w.a.list_audit(limit=500)),
    "list_members": lambda w: _equal(w.a.list_members(), []),
    "list_invitations": lambda w: _equal(w.a.list_invitations(), []),
    "set_member_role": lambda w: missing(
        lambda: w.a.set_member_role(_b_member(w), "viewer"), "member_not_found"
    ),
    "remove_member": lambda w: missing(lambda: w.a.remove_member(_b_member(w)), "member_not_found"),
    "create_invitation": lambda w: (
        _equal(w.b.list_invitations()[0]["email"], "someone@example.com")
        if w.a.create_invitation("x@example.com", "viewer") and _b_invitation(w)
        else None
    ),
    "revoke_invitation": lambda w: missing(
        lambda: w.a.revoke_invitation(_b_invitation(w)), "invitation_not_found"
    ),
    "workspace_status": lambda w: _equal(w.a.workspace_status()["id"], "a"),
    "delete_workspace": lambda w: (
        refused_422(lambda: w.a.delete_workspace("b")),  # naming B's id does not delete B
        _equal(w.b.workspace_status()["deleted_at"], None),
    ),
    "restore_workspace": lambda w: missing_409(lambda: w.a.restore_workspace()),
    "usage": lambda w: _equal(w.a.usage()["used"]["entries"], 1),  # its own one, not B's
    "recount_usage": lambda w: (
        w.a.recount_usage(),
        _equal(w.b.usage()["used"]["entries"], 1),
        _equal(
            w.service.db.query("SELECT entries FROM workspace_usage WHERE workspace_id='b'")[0][
                "entries"
            ],
            1,
        ),
    ),
    "prune_audit": lambda w: (w.a.prune_audit(), _equal(len(w.b.list_audit()) > 0, True)),
    "revoke_token": lambda w: missing(lambda: w.a.revoke_token(_b_token(w)), "token_not_found"),
    "rotate_token": lambda w: missing(lambda: w.a.rotate_token(_b_token(w)), "token_not_found"),
}

# Methods that do not take tenant data, or that are maintenance for the whole database.
EXEMPT = {
    "authenticate_token": "finds the workspace from the credential, so no tenant exists yet",
    "workspaces_of": "a person's workspaces: people are global",
    "membership_of": "a person's role in a named workspace: people are global",
    "principal_for_session": "a signed-in person's identity: global",
    "switch_workspace": "a person's own session: checks their membership",
    "switch_session": "a person's own session: checks their membership",
    "invitation_preview": "found by its secret token, before a workspace is known",
    "accept_invitation": "found by its secret token, before a workspace is known",
    "purge_workspaces": "the operator's schedule: purges deleted workspaces past their grace",
    "sign_in_identity": "people are global, not tenant data (ADR-0011 section 3)",
    "create_session": "a person's sign-in: global",
    "authenticate_session": "finds the person from a cookie: global",
    "end_session": "a person's sign-in: global",
    "end_session_id": "a person's sign-in by id: global",
    "end_sessions_of": "a person's sign-ins: global",
    "for_scope": "builds a scoped view",
    "start_cluster": "process-level: joins the other replicas' event channel",
    "stop_cluster": "process-level: leaves it",
    "owns": "says whether an event belongs to this workspace",
    "create_workspace": "creates the tenant itself",
    "set_mode_default": "",  # placeholder removed below
    "sync_index": "rebuilds the lexical index of this workspace's entries only",
    "sync_embeddings": "maintenance of this workspace's vectors",
    "log_call": "internal: the caller has already resolved the project in its own scope",
}
del EXEMPT["set_mode_default"]


def refused_422(call: Callable[[], Any]) -> None:
    with pytest.raises(SemulaError) as caught:
        call()
    assert caught.value.status == 422


def missing_409(call: Callable[[], Any]) -> None:
    with pytest.raises(SemulaError) as caught:
        call()
    assert caught.value.status == 409


def _b_member(w: World) -> str:
    from semula.oidc import Identity

    person = w.service.sign_in_identity(
        Identity("https://idp.test", "m1", "member@example.com", True, "M")
    )
    secret = w.b.create_invitation("member@example.com", "admin")["token"]
    w.service.accept_invitation(secret, person["id"])
    return str(person["id"])


def _b_invitation(w: World) -> str:
    existing = w.b.list_invitations()
    if existing:
        return str(existing[0]["id"])
    return str(w.b.create_invitation("someone@example.com", "viewer")["id"])


def _b_token(w: World) -> str:
    return str(w.b.create_token("b's token", "admin", project="shop")["id"])


def _equal(got: Any, want: Any) -> None:
    assert got == want, f"{got!r} != {want!r}"


def _no_answer_for_a(w: World) -> None:
    got = asyncio.run(w.a.wait_for_answer(w.pending, 0.2))
    assert got is None, "workspace A read workspace B's answer through a pending id"


def _same_prompt_is_a_new_entry(w: World) -> None:
    body = {"messages": [{"role": "user", "content": "b's secret prompt"}]}
    entry, created = w.a.upsert_entry("mine", body, {"content": "a's version"})
    assert created and entry["id"] != w.entry
    assert w.b.get_entry(w.entry)["responses"][0]["message"]["content"] == "b's secret answer"


def _import_into_a(w: World) -> None:
    report = w.a.import_project(w.bundle, "imported")
    assert report["entries"]["created"] == 1
    assert w.b.get_entry(w.entry)["project"] == "shop"


def test_every_public_service_method_is_covered() -> None:
    public = {
        name
        for name, member in inspect.getmembers(Service, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == set(SCENARIOS) | set(EXEMPT), (
        "a service method is missing from the cross-tenant matrix: "
        f"{sorted(public ^ (set(SCENARIOS) | set(EXEMPT)))}"
    )


@pytest.mark.parametrize("method", sorted(SCENARIOS))
def test_workspace_a_cannot_reach_workspace_b_through(world: World, method: str) -> None:
    SCENARIOS[method](world)
    # Whatever A tried, B's data is exactly as it was.
    assert world.b.get_entry(world.entry)["version"] == 1
    assert world.b.get_pending(world.pending)["status"] in ("pending", "answered")
    assert [p["id"] for p in world.b.list_projects()] == ["default", "shop"]


def test_events_name_their_workspace_and_a_waiter_ignores_other_workspaces(world: World) -> None:
    async def go() -> None:
        with world.service.events.subscribe() as sub:
            world.b.open_pending(req("b parks another"), "shop")
            event = await asyncio.wait_for(sub.get(), 2)
        assert event == {"type": "queues", "project": "shop", "workspace": "b"}
        assert world.a.owns(event) is False and world.b.owns(event) is True

        started = asyncio.get_running_loop().time()
        waiter = asyncio.create_task(world.a.wait_for_pending(None, 0.6))
        await asyncio.sleep(0.2)
        world.b.open_pending(req("b parks while a waits"), "shop")  # not A's call to answer
        assert await waiter == []
        assert asyncio.get_running_loop().time() - started >= 0.55, "a waiter was woken by B"

        woken = asyncio.create_task(world.a.wait_for_pending("mine", 5))
        await asyncio.sleep(0.2)
        world.a.open_pending(req("a parks"), "mine")
        assert len(await asyncio.wait_for(woken, 2)) == 1

    asyncio.run(go())


async def test_the_routes_serve_each_workspace_only_its_own_data(tmp_path: Path) -> None:
    """The same app answers as workspace A or B, chosen here by a header standing in for the
    credential middleware of the hosted build."""
    import httpx
    from starlette.requests import Request

    from semula.app import create_app

    app = create_app(
        Settings(data_dir=tmp_path, mode="replay", database_url=fresh_database_url(tmp_path))
    )
    service: Service = app.state.service
    service.create_workspace("a")
    service.create_workspace("b")

    @app.middleware("http")
    async def credential(request: Request, call_next: Any) -> Any:
        request.state.scope = Scope(request.headers.get("x-test-workspace", "default"))
        return await call_next(request)

    b = service.for_scope(Scope("b"))
    b.create_project("shop")
    entry, _ = b.upsert_entry(
        "shop",
        {"messages": [{"role": "user", "content": "only b knows"}]},
        {"content": "b's answer"},
    )
    asked = {"model": "m", "messages": [{"role": "user", "content": "only b knows"}]}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        as_b = {"x-test-workspace": "b"}
        as_a = {"x-test-workspace": "a"}
        got = await client.post("/v1/projects/shop/chat/completions", json=asked, headers=as_b)
        assert got.json()["choices"][0]["message"]["content"] == "b's answer"

        for path in (
            f"/api/v1/entries/{entry['id']}",
            f"/api/v1/entries/{entry['id']}/history",
            "/api/v1/projects/shop",
            "/api/v1/projects/shop/entries",
            "/api/v1/projects/shop/export",
        ):
            assert (await client.get(path, headers=as_a)).status_code == 404, path
        projects = (await client.get("/api/v1/projects", headers=as_a)).json()["projects"]
        assert [p["id"] for p in projects] == ["default"]  # its own, nothing of B's
        assert (await client.get("/api/v1/log", headers=as_a)).json()["calls"] == []
        miss = await client.post("/v1/projects/shop/chat/completions", json=asked, headers=as_a)
        assert miss.status_code == 404, "A has no project 'shop', so nothing of B's is reachable"
        assert (await client.get("/api/v1/projects/shop/entries", headers=as_b)).json()[
            "total"
        ] == 1


def test_the_same_ids_in_two_workspaces_never_touch_each_other(world: World) -> None:
    """Import keeps ULIDs, so workspace A can hold entries, aliases and revisions with exactly
    the ids workspace B has (the composite keys of ADR-0011). Every write, read and cascade by
    id must act on its own workspace's row only."""
    from semula.lexical import Fts5Index

    report = world.a.import_project(world.bundle, "twin")
    assert report["entries"]["created"] == 1 and report["aliases"]["created"] == 1
    twin = world.a.get_entry(world.entry)
    assert twin["id"] == world.entry and twin["project"] == "twin"
    alias_id = world.bundle["aliases"][0]["id"]

    # a write by id changes the caller's row only, and each keeps its own history
    world.a.update_entry(world.entry, 1, {"response": {"content": "a's rewrite"}})
    assert world.b.get_entry(world.entry)["version"] == 1
    assert (
        world.b.get_entry(world.entry)["responses"][0]["message"]["content"] == "b's secret answer"
    )
    assert [r["version"] for r in world.a.entry_history(world.entry)["revisions"]] == [2, 1]
    assert [r["version"] for r in world.b.entry_history(world.entry)["revisions"]] == [1]

    # hits are counted per workspace
    world.a.log_call("hit", 0.0, project="twin", entry_id=world.entry, count_hit=True)
    assert world.a.get_entry(world.entry)["hit_count"] == 1
    assert world.b.get_entry(world.entry)["hit_count"] == 0

    # a review decision by id is the caller's
    world.a.record_candidates(req("b's secret prompts"), "twin", [Candidate(world.entry, 0.8)])
    (own,) = world.a.list_candidates("twin")
    world.a.reject_candidate(own["id"])
    assert [c["status"] for c in world.b.list_candidates("shop", "open")] == ["open"]

    # both workspaces serve their own answer for the same words
    served_a = world.a.lookup(req("b's secret prompt"), "twin").match
    served_b = world.b.lookup(req("b's secret prompt"), "shop").match
    assert served_a is not None and served_b is not None
    assert served_a.stored["message"]["content"] == "a's rewrite"
    assert served_b.stored["message"]["content"] == "b's secret answer"

    # deleting A's entry removes A's aliases, history and index rows, and none of B's
    world.a.delete_entry(world.entry, 2)
    assert world.b.get_entry(world.entry)["version"] == 1
    assert [r["version"] for r in world.b.entry_history(world.entry)["revisions"]] == [1]
    assert world.b.export_project("shop")["aliases"][0]["id"] == alias_id
    index = world.service.lexical
    if isinstance(index, Fts5Index):
        assert world.entry in index.indexed_ids("b")
        assert world.entry not in index.indexed_ids("a")
    assert world.b.lookup(req("b's secret prompt"), "shop").match is not None
