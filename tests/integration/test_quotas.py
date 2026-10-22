"""Quotas and limits of a hosted workspace (ADR-0011 section 6, SEM-88)."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from semula import canonical
from semula.app import create_app
from semula.config import Quotas, Settings
from semula.db import Database
from semula.errors import SemulaError
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration


def build(tmp_path: Path, **limits: Any) -> Service:
    settings = Settings(data_dir=tmp_path, hosted=True, quotas=Quotas(**limits))
    svc = Service(Database(fresh_database_url(tmp_path)), settings)
    svc.create_workspace("a")
    svc.create_workspace("b")
    return svc


def body(text: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


def refused(call: Any, status: int, code: str) -> SemulaError:
    with pytest.raises(SemulaError) as caught:
        call()
    assert (caught.value.status, caught.value.code) == (status, code), caught.value.message
    return caught.value


def test_projects_are_limited_per_workspace_and_a_delete_frees_one(tmp_path: Path) -> None:
    svc = build(tmp_path, projects=3)
    a = svc.for_scope(Scope("a"))  # the default project is the first of three
    a.create_project("one")
    a.create_project("two")
    err = refused(lambda: a.create_project("three"), 403, "quota_exceeded")
    assert err.extra["quota"] == {"name": "projects", "limit": 3, "used": 3}
    svc.for_scope(Scope("b")).create_project("elsewhere")  # another workspace has its own count
    a.delete_project("two")
    a.create_project("three")


def test_entries_are_limited_however_they_arrive_and_deleting_frees_room(tmp_path: Path) -> None:
    svc = build(tmp_path, entries=2)
    a = svc.for_scope(Scope("a"))
    first, _ = a.upsert_entry("default", body("one"), {"content": "1"})
    a.upsert_entry("default", body("two"), {"content": "2"})
    refused(
        lambda: a.upsert_entry("default", body("three"), {"content": "3"}), 403, "quota_exceeded"
    )
    pending = a.open_pending(canonical.from_openai(body("four")), "default")
    refused(lambda: a.answer_pending(pending, {"content": "4"}), 403, "quota_exceeded")
    assert a.get_pending(pending)["status"] == "pending"  # still waiting: nothing was lost
    bundle = svc.for_scope(Scope("b")).export_project("default")
    other = svc.for_scope(Scope("b"))
    for n in range(2):
        other.upsert_entry("default", body(f"b{n}"), {"content": "x"})
    refused(
        lambda: a.import_project(other.export_project("default"), "imported"), 403, "quota_exceeded"
    )
    assert a.list_projects()  # the failed import created nothing half-way
    assert "imported" not in [p["id"] for p in a.list_projects()]
    a.delete_entry(first["id"], 1)
    a.answer_pending(pending, {"content": "4"})
    assert bundle["project"]["id"] == "default"


def test_stored_bytes_are_limited(tmp_path: Path) -> None:
    svc = build(tmp_path, bytes=1500)
    a = svc.for_scope(Scope("a"))
    a.upsert_entry("default", body("small"), {"content": "ok"})
    refused(
        lambda: a.upsert_entry("default", body("big"), {"content": "x" * 3000}),
        403,
        "quota_exceeded",
    )
    entry = a.list_entries("default")["entries"][0]
    refused(
        lambda: a.update_entry(entry["id"], 1, {"response": {"content": "y" * 3000}}),
        403,
        "quota_exceeded",
    )
    assert a.usage()["used"]["bytes"] < 1500


def test_parked_calls_are_limited_per_project_and_per_workspace(tmp_path: Path) -> None:
    svc = build(tmp_path, parked_per_project=2, parked_per_workspace=3)
    a = svc.for_scope(Scope("a"))
    a.create_project("other")
    for n in range(2):
        a.open_pending(canonical.from_openai(body(f"d{n}")), "default")
    err = refused(
        lambda: a.open_pending(canonical.from_openai(body("d2")), "default"), 429, "too_many_parked"
    )
    assert err.extra["quota"]["name"] == "parked_per_project"
    a.open_pending(canonical.from_openai(body("o0")), "other")
    err = refused(
        lambda: a.open_pending(canonical.from_openai(body("o1")), "other"), 429, "too_many_parked"
    )
    assert err.extra["quota"]["name"] == "parked_per_workspace"
    same = a.open_pending(
        canonical.from_openai(body("d0")), "default"
    )  # joining one is not parking
    assert same


def test_tokens_are_limited(tmp_path: Path) -> None:
    svc = build(tmp_path, tokens=2)
    a = svc.for_scope(Scope("a"))
    first = a.create_token("one", "replay")
    a.create_token("two", "replay")
    refused(lambda: a.create_token("three", "replay"), 403, "quota_exceeded")
    a.revoke_token(first["id"])  # a revoked token no longer counts
    a.create_token("three", "replay")


def test_the_counters_match_what_is_stored_and_a_recount_repairs_drift(tmp_path: Path) -> None:
    svc = build(tmp_path, entries=100)
    a = svc.for_scope(Scope("a"))
    a.create_project("shop")
    a.upsert_entry("shop", body("one"), {"content": "1"})
    entry, _ = a.upsert_entry("shop", body("two"), {"content": "22"})
    a.update_entry(entry["id"], 1, {"response": {"content": "a much longer answer"}})
    a.import_project(a.export_project("shop"))
    used = a.usage()["used"]
    assert (used["projects"], used["entries"]) == (2, 2)
    real = a.recount_usage()
    assert real == a.usage()["used"]
    a.delete_project("shop")
    assert (
        a.usage()["used"] == {**a.recount_usage(), "parked": 0}
        and a.usage()["used"]["entries"] == 0
    )
    svc.db.run("UPDATE workspace_usage SET entries = 99, bytes = 12345")
    assert a.usage()["used"]["entries"] == 99
    assert a.recount_usage()["entries"] == 0 and a.usage()["used"]["entries"] == 0


def test_nothing_is_limited_locally(tmp_path: Path) -> None:
    svc = Service(Database(fresh_database_url(tmp_path)), Settings(data_dir=tmp_path))
    for n in range(30):
        svc.create_project(f"p{n}")
    assert svc.usage()["limits"]["projects"] is None


def test_replay_hits_do_not_touch_the_counters(tmp_path: Path) -> None:
    svc = build(tmp_path, entries=10)
    a = svc.for_scope(Scope("a"))
    entry, _ = a.upsert_entry("default", body("hit me"), {"content": "x"})
    before = svc.db.query("SELECT * FROM workspace_usage WHERE workspace_id = 'a'")[0]
    for _ in range(5):
        a.log_call("hit", 0.0, project="default", entry_id=entry["id"], count_hit=True)
    assert svc.db.query("SELECT * FROM workspace_usage WHERE workspace_id = 'a'")[0] == before


def test_a_long_record_timeout_is_refused_when_hosted(tmp_path: Path) -> None:
    svc = Service(
        Database(fresh_database_url(tmp_path)),
        Settings(data_dir=tmp_path, hosted=True, quotas=Quotas(record_timeout_max=900.0)),
    )
    svc.create_workspace("a")
    a = svc.for_scope(Scope("a"))
    refused_with = None
    with pytest.raises(SemulaError) as caught:
        a.update_project("default", settings={"record_timeout": 901})
    refused_with = caught.value
    assert refused_with.status == 422 and refused_with.param == "settings.record_timeout"
    a.update_project("default", settings={"record_timeout": 600})


async def test_a_body_over_the_limit_is_refused_and_so_is_a_burst_from_one_token(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        hosted=True,
        database_url=fresh_database_url(tmp_path),
        quotas=Quotas(body_bytes=2000, rate=5.0, burst=10),
    )
    app = create_app(settings)
    svc: Service = app.state.service
    svc.create_workspace("a")
    secret = svc.for_scope(Scope("a")).create_token("t", "replay")["token"]
    headers = {"Authorization": f"Bearer {secret}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        big = {"model": "m", "messages": [{"role": "user", "content": "x" * 5000}]}
        resp = await c.post("/v1/chat/completions", json=big, headers=headers)
        assert resp.status_code == 413 and resp.json()["error"]["code"] == "request_too_large"
        statuses = [(await c.get("/v1/models", headers=headers)).status_code for _ in range(30)]
        assert (
            statuses.count(200) >= 9 and 429 in statuses
        )  # ten in the bucket, one spent on the big body
        limited = await c.get("/v1/models", headers=headers)
        assert limited.status_code == 429 and float(limited.headers["retry-after"]) > 0
        await asyncio.sleep(1.0)  # five tokens come back each second
        assert (await c.get("/v1/models", headers=headers)).status_code == 200
