"""A token bound to one project reaches nothing of the workspace's other projects (ADR-0010).

Every public service method is called with a service bound to project ``shop`` and the ids or
names of project ``other``: a 404 as for something that does not exist, or a list without it.
A new public method that is not listed fails the coverage test.
"""

import asyncio
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from semula import canonical
from semula.config import Settings
from semula.db import Database
from semula.errors import SemulaError
from semula.scope import Principal, Scope
from semula.service import Candidate, Service
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration


def req(text: str) -> Any:
    return canonical.from_openai({"messages": [{"role": "user", "content": text}]})


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.service = Service(Database(fresh_database_url(tmp_path)), Settings(data_dir=tmp_path))
        self.service.create_workspace("a")
        self.admin = self.service.for_scope(Scope("a"))  # the whole workspace
        for project in ("shop", "other"):
            self.admin.create_project(project)
        self.mine: dict[str, str] = self._seed("shop")
        self.theirs: dict[str, str] = self._seed("other")
        token = Principal(kind="token", id="t", workspace_id="a", scope="admin", project="shop")
        self.bound = self.service.for_scope(Scope("a", token))

    def _seed(self, project: str) -> dict[str, str]:
        entry, _ = self.admin.upsert_entry(
            project,
            {"messages": [{"role": "user", "content": f"{project} prompt"}]},
            {"content": project},
        )
        self.admin.record_candidates(
            req(f"{project} prompts"), project, [Candidate(entry["id"], 0.8)]
        )
        (cand,) = self.admin.list_candidates(project)
        pending = self.admin.open_pending(req(f"{project} is waiting"), project)
        self.admin.log_call(
            "hit", 0.0, project=project, entry_id=entry["id"], req=req(f"{project} prompt")
        )
        return {"entry": entry["id"], "candidate": cand["id"], "pending": pending}


def missing(call: Callable[[], Any]) -> None:
    with pytest.raises(SemulaError) as caught:
        call()
    assert caught.value.status == 404, f"{caught.value.status} {caught.value.code}"


def only_shop(items: list[dict[str, Any]], key: str = "project") -> None:
    assert items and {i[key] for i in items} == {"shop"}, items


def forbidden(call: Callable[[], Any]) -> None:
    with pytest.raises(SemulaError) as caught:
        call()
    assert caught.value.status == 403, f"{caught.value.status} {caught.value.code}"


S: dict[str, Callable[[World], None]] = {
    "get_entry": lambda w: missing(lambda: w.bound.get_entry(w.theirs["entry"])),
    "entry_history": lambda w: missing(lambda: w.bound.entry_history(w.theirs["entry"])),
    "update_entry": lambda w: missing(
        lambda: w.bound.update_entry(w.theirs["entry"], 1, {"response": {"content": "x"}})
    ),
    "delete_entry": lambda w: missing(lambda: w.bound.delete_entry(w.theirs["entry"], 1)),
    "get_candidate": lambda w: missing(lambda: w.bound.get_candidate(w.theirs["candidate"])),
    "approve_candidate": lambda w: missing(
        lambda: w.bound.approve_candidate(w.theirs["candidate"])
    ),
    "reject_candidate": lambda w: missing(lambda: w.bound.reject_candidate(w.theirs["candidate"])),
    "get_pending": lambda w: missing(lambda: w.bound.get_pending(w.theirs["pending"])),
    "answer_pending": lambda w: missing(
        lambda: w.bound.answer_pending(w.theirs["pending"], {"content": "x"})
    ),
    "get_project": lambda w: (
        missing(lambda: w.bound.get_project("other")),
        _equal(w.bound.get_project("shop")["id"], "shop"),
    ),
    "require_project": lambda w: missing(lambda: w.bound.require_project("other")),
    "project_settings": lambda w: missing(lambda: w.bound.project_settings("other")),
    "mode_of": lambda w: missing(lambda: w.bound.mode_of("other")),
    "record_timeout": lambda w: missing(lambda: w.bound.record_timeout("other")),
    "list_models": lambda w: missing(lambda: w.bound.list_models("other")),
    "update_project": lambda w: missing(lambda: w.bound.update_project("other", name="x")),
    "delete_project": lambda w: missing(lambda: w.bound.delete_project("other")),
    "export_project": lambda w: missing(lambda: w.bound.export_project("other")),
    "list_entries": lambda w: missing(lambda: w.bound.list_entries("other")),
    "upsert_entry": lambda w: missing(
        lambda: w.bound.upsert_entry(
            "other", {"messages": [{"role": "user", "content": "x"}]}, {"content": "x"}
        )
    ),
    "open_pending": lambda w: missing(lambda: w.bound.open_pending(req("x"), "other")),
    "record_candidates": lambda w: missing(
        lambda: w.bound.record_candidates(req("x"), "other", [Candidate(w.theirs["entry"], 0.9)])
    ),
    "test_match": lambda w: missing(lambda: w.bound.test_match(req("other prompt"), "other")),
    "find": lambda w: missing(lambda: w.bound.find(req("other prompt"), "other")),
    "lookup": lambda w: missing(lambda: w.bound.lookup(req("other prompt"), "other")),
    "list_projects": lambda w: _equal([p["id"] for p in w.bound.list_projects()], ["shop"]),
    "list_pending": lambda w: (
        only_shop(w.bound.list_pending("pending")),
        only_shop(w.bound.list_pending(None)),
        missing(lambda: w.bound.list_pending("pending", "other")),
    ),
    "wait_for_pending": lambda w: (
        only_shop(asyncio.run(w.bound.wait_for_pending(None, 0.2))),
        missing(lambda: asyncio.run(w.bound.wait_for_pending("other", 0.2))),
    ),
    "list_candidates": lambda w: (
        only_shop(w.bound.list_candidates()),
        only_shop(w.bound.list_candidates(None, None)),
        missing(lambda: w.bound.list_candidates("other")),
    ),
    "list_calls": lambda w: (
        only_shop(w.bound.list_calls()),
        missing(lambda: w.bound.list_calls(project="other")),
    ),
    "log_stats": lambda w: (
        _equal(w.bound.log_stats()["totals"]["calls"], 1),
        missing(lambda: w.bound.log_stats("other")),
    ),
    "queue_counts": lambda w: (
        _equal(w.bound.queue_counts(), {"pending": 1, "review": 1}),
        missing(lambda: w.bound.queue_counts("other")),
    ),
    "import_project": lambda w: (
        missing(lambda: w.bound.import_project(w.admin.export_project("other"))),
        missing(lambda: w.bound.import_project(w.admin.export_project("other"), "other")),
    ),
    "create_project": lambda w: forbidden(lambda: w.bound.create_project("brand-new")),
    "create_token": lambda w: forbidden(lambda: w.bound.create_token("t", "record")),
    "list_tokens": lambda w: forbidden(lambda: w.bound.list_tokens()),
    "list_audit": lambda w: forbidden(lambda: w.bound.list_audit()),
    "usage": lambda w: forbidden(lambda: w.bound.usage()),
    "workspace_status": lambda w: _equal(w.bound.workspace_status()["id"], "a"),
    "delete_workspace": lambda w: forbidden(lambda: w.bound.delete_workspace("a")),
    "restore_workspace": lambda w: forbidden(lambda: w.bound.restore_workspace()),
    "recount_usage": lambda w: forbidden(lambda: w.bound.recount_usage()),
    "revoke_token": lambda w: forbidden(lambda: w.bound.revoke_token("x")),
    "rotate_token": lambda w: forbidden(lambda: w.bound.rotate_token("x")),
    "set_mode": lambda w: (
        missing(lambda: w.bound.set_mode("record", "other")),
        forbidden(lambda: w.bound.set_mode("record")),  # the process default is the operator's
    ),
}
EXEMPT = {
    "for_scope", "owns", "create_workspace", "authenticate_token", "sync_index",
    "sync_embeddings", "prune_log", "log_call", "embeddings_status", "wait_for_answer",
    "prune_audit", "purge_workspaces",
}  # fmt: skip


def _equal(got: Any, want: Any) -> None:
    assert got == want, f"{got!r} != {want!r}"


def test_every_public_service_method_is_covered() -> None:
    public = {
        n for n, m in inspect.getmembers(Service, inspect.isfunction) if not n.startswith("_")
    }
    assert public == set(S) | EXEMPT, (
        f"not in the project-binding matrix: {sorted(public ^ (set(S) | EXEMPT))}"
    )


@pytest.mark.parametrize("method", sorted(S))
def test_a_project_bound_service_cannot_reach_another_project_through(
    tmp_path: Path, method: str
) -> None:
    world = World(tmp_path)
    S[method](world)
    assert world.admin.get_entry(world.theirs["entry"])["version"] == 1  # untouched
    assert {p["id"] for p in world.admin.list_projects()} == {"default", "shop", "other"}


def test_a_bound_service_still_does_its_own_projects_work(tmp_path: Path) -> None:
    w = World(tmp_path)
    assert w.bound.get_entry(w.mine["entry"])["project"] == "shop"
    assert w.bound.list_entries("shop")["total"] == 1
    w.bound.answer_pending(w.mine["pending"], {"content": "mine"})
    w.bound.approve_candidate(w.mine["candidate"])
    assert (
        w.bound.update_entry(w.mine["entry"], 1, {"response": {"content": "edited"}})["version"]
        == 2
    )
