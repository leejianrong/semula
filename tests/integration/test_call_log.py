"""The call log, its retention, and the live event stream (SEM-34, SEM-35)."""

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from semula.events import QUEUE_SIZE, EventBus
from semula.schema import iso
from tests.integration.conftest import chat, make_client
from tests.integration.test_replay import record_answer
from tests.support.server import running_server

pytestmark = pytest.mark.integration


async def seed(client: httpx.AsyncClient, text: str = "hello") -> None:
    resp = await client.post(
        "/api/v1/projects/default/entries",
        json={"request": chat(text), "response": {"content": "hi"}},
    )
    assert resp.status_code == 201


# --- SEM-34 ---------------------------------------------------------------------------------
async def test_every_call_is_logged_with_its_outcome_tier_score_and_latency(
    replay: httpx.AsyncClient,
) -> None:
    await seed(replay)
    await replay.post("/v1/chat/completions", json=chat("hello"))
    await replay.post("/v1/chat/completions", json=chat("nothing like it"))
    calls = (await replay.get("/api/v1/log")).json()["calls"]
    assert [c["outcome"] for c in calls] == ["miss", "hit"]  # newest first
    hit = calls[1]
    assert hit["tier"] == "exact" and hit["project"] == "default" and hit["mode"] == "replay"
    assert hit["entry_id"] and hit["latency_ms"] >= 0 and hit["ts"].endswith("Z")
    only_hits = (await replay.get("/api/v1/log", params={"outcome": "hit"})).json()["calls"]
    assert [c["id"] for c in only_hits] == [hit["id"]]
    older = (await replay.get("/api/v1/log", params={"before_id": calls[0]["id"]})).json()["calls"]
    assert [c["id"] for c in older] == [hit["id"]]


async def test_the_log_can_be_filtered_by_project(replay: httpx.AsyncClient) -> None:
    await replay.post("/api/v1/projects", json={"id": "other"})
    await replay.post("/v1/projects/other/chat/completions", json=chat("x"))
    await replay.post("/v1/chat/completions", json=chat("y"))
    other = (await replay.get("/api/v1/log", params={"project": "other"})).json()["calls"]
    assert [c["project"] for c in other] == ["other"]
    assert (await replay.get("/api/v1/log", params={"project": "nope"})).status_code == 404


def every_stored_value(client: httpx.AsyncClient) -> str:
    """All data in every table, as one string, for 'this never reached the database' checks."""
    db = client._transport.app.state.db  # type: ignore[attr-defined]
    parts: list[str] = []
    with db.engine.connect() as conn:
        for name in sa.inspect(conn).get_table_names():
            if name.startswith("entry_fts"):
                continue
            table = sa.Table(name, sa.MetaData(), autoload_with=conn)
            parts.extend(str(r) for r in conn.execute(sa.select(table)))
    return "\n".join(parts)


async def test_no_authorization_header_is_stored(tmp_path: Path) -> None:
    async with make_client(tmp_path, mode="record", record_timeout=5.0, api_key="topsecret") as c:
        headers = {"Authorization": "Bearer topsecret", "X-Semula-Project": "default"}
        call = asyncio.create_task(
            c.post("/v1/chat/completions", json=chat("who"), headers=headers)
        )
        for _ in range(100):
            pending = (await c.get("/api/v1/pending", headers=headers)).json()["pending"]
            if pending:
                break
            await asyncio.sleep(0.01)
        await c.post(
            f"/api/v1/pending/{pending[0]['id']}/answer", json={"content": "me"}, headers=headers
        )
        assert (await call).status_code == 200
        assert "topsecret" not in every_stored_value(c)
        assert "Bearer" not in every_stored_value(c)


async def test_retention_drops_old_rows_and_caps_the_count(tmp_path: Path) -> None:
    async with make_client(tmp_path, mode="replay", log_retention_days=30, log_max_rows=5) as c:
        service = c._transport.app.state.service  # type: ignore[attr-defined]
        db = service.db
        old = iso(datetime.now(UTC).replace(year=2020))
        for _ in range(3):
            db.run(
                "INSERT INTO call_log (workspace_id, project_id, ts, mode, outcome) "
                "VALUES ('default', 'default', :ts, 'replay', 'miss')",
                {"ts": old},
            )
        for _ in range(8):
            await c.post("/v1/chat/completions", json=chat("fresh"))
        assert service.prune_log() > 0
        rows = db.query("SELECT ts, id FROM call_log ORDER BY id")
        assert len(rows) == 5  # the three from 2020 are gone, and the cap keeps the newest 5
        assert all(r["ts"] > old for r in rows)
        assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
        newest = db.query_one("SELECT MAX(id) AS m FROM call_log")["m"]  # type: ignore[index]
        assert rows[-1]["id"] == newest


async def test_retention_runs_by_itself_while_calls_arrive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("semula.service.PRUNE_EVERY", 5)
    async with make_client(tmp_path, mode="replay", log_max_rows=10) as c:
        service = c._transport.app.state.service  # type: ignore[attr-defined]
        for _ in range(30):
            await c.post("/v1/chat/completions", json=chat("x"))
        count = service.db.query_one("SELECT COUNT(*) AS n FROM call_log")["n"]
        assert count <= 14  # the cap of 10 plus at most one sweep interval of overshoot


# --- SEM-35 ---------------------------------------------------------------------------------
async def test_a_subscriber_sees_calls_from_other_threads() -> None:
    bus = EventBus()
    with bus.subscribe() as sub:
        threading.Thread(target=bus.publish, args=({"type": "call", "n": 1},)).start()
        assert (await asyncio.wait_for(sub.get(), 1))["n"] == 1
    assert bus.subscribers == 0


async def test_a_slow_consumer_never_blocks_the_publisher_and_loses_only_the_oldest() -> None:
    bus = EventBus()
    with bus.subscribe() as sub:
        started = time.perf_counter()
        for n in range(QUEUE_SIZE * 4):
            bus.publish({"type": "call", "n": n})  # nobody is reading
        assert time.perf_counter() - started < 1
        await asyncio.sleep(0.1)  # let the loop deliver
        kept = [sub.queue.get_nowait()["n"] for _ in range(sub.queue.qsize())]
        assert kept == list(range(QUEUE_SIZE * 3, QUEUE_SIZE * 4))  # the newest survive
        assert sub.dropped == QUEUE_SIZE * 3


def test_the_publisher_forgets_a_subscriber_whose_loop_has_gone() -> None:
    bus = EventBus()

    async def subscribe() -> None:
        bus.subscribe()

    asyncio.run(subscribe())  # the loop closes with the subscription still registered
    bus.publish({"type": "call"})  # must not raise
    assert bus.subscribers == 0


def stream_lines(url: str, path: str, lines: list[str], ready: threading.Event) -> None:
    with httpx.stream("GET", url + path, timeout=httpx.Timeout(5.0, read=10.0)) as resp:
        for line in resp.iter_lines():
            lines.append(line)
            if line == ": connected":
                ready.set()
            if line.startswith("data: "):
                return


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    with running_server(tmp_path, mode="replay") as url:
        yield url


def test_the_stream_shows_a_call_within_a_second(server: str) -> None:
    lines: list[str] = []
    ready = threading.Event()
    t = threading.Thread(target=stream_lines, args=(server, "/api/v1/log/stream", lines, ready))
    t.start()
    assert ready.wait(5)
    started = time.perf_counter()
    httpx.post(server + "/v1/chat/completions", json=chat("anyone there"))
    t.join(5)
    assert time.perf_counter() - started < 1
    assert "event: call" in lines
    event = json.loads(next(line for line in lines if line.startswith("data: "))[6:])
    assert event["outcome"] == "miss" and event["project"] == "default"


def test_the_stream_can_be_limited_to_one_project(server: str) -> None:
    httpx.post(server + "/api/v1/projects", json={"id": "other"})
    lines: list[str] = []
    ready = threading.Event()
    t = threading.Thread(
        target=stream_lines, args=(server, "/api/v1/log/stream?project=other", lines, ready)
    )
    t.start()
    assert ready.wait(5)
    httpx.post(server + "/v1/chat/completions", json=chat("default project"))
    httpx.post(server + "/v1/projects/other/chat/completions", json=chat("other project"))
    t.join(5)
    event = json.loads(next(line for line in lines if line.startswith("data: "))[6:])
    assert event["project"] == "other"


def test_a_stuck_stream_reader_does_not_slow_the_proxy(server: str) -> None:
    # Open a stream and never read from it, then send more calls than its queue holds.
    stuck = httpx.stream("GET", server + "/api/v1/log/stream", timeout=10.0)
    response = stuck.__enter__()
    try:
        started = time.perf_counter()
        with httpx.Client() as client:
            for _ in range(QUEUE_SIZE + 100):
                resp = client.post(server + "/v1/chat/completions", json=chat("x"))
                assert resp.status_code == 424
        assert time.perf_counter() - started < 30
    finally:
        response.close()
        stuck.__exit__(None, None, None)


def test_the_stream_requires_the_api_key(tmp_path: Path) -> None:
    with running_server(tmp_path, api_key="k") as url:
        assert httpx.get(url + "/api/v1/log/stream").status_code == 401


async def test_recorded_answers_are_logged_too(record: httpx.AsyncClient) -> None:
    await record_answer(record, chat("log me"), {"content": "ok"})
    outcomes = [c["outcome"] for c in (await record.get("/api/v1/log")).json()["calls"]]
    assert outcomes == ["recorded"]


# --- SEM-63: a redacted preview of the prompt on every row -------------------------------------
HIT_STATEMENTS = 5  # measured before SEM-63: a hit looks up, then logs
SECRET_KEY = "sk-" + "ant-api03-AbCdEfGhIjKlMnOpQrStUvWx"  # noqa: S105


async def test_each_outcome_carries_a_preview_of_the_prompt(record: httpx.AsyncClient) -> None:
    await seed(record, "hello")
    await record.post("/v1/chat/completions", json=chat("hello"))  # a hit
    call = asyncio.create_task(record.post("/v1/chat/completions", json=chat("park me please")))
    for _ in range(200):
        pending = (await record.get("/api/v1/pending")).json()["pending"]
        if pending:
            break
        await asyncio.sleep(0.01)
    await record.post(f"/api/v1/pending/{pending[0]['id']}/answer", json={"content": "ok"})
    await call
    await record.put("/api/v1/mode", json={"mode": "replay"})
    await record.post("/v1/chat/completions", json=chat("a miss " + "z" * 200))
    calls = (await record.get("/api/v1/log")).json()["calls"]
    previews = {c["outcome"]: c["preview"] for c in calls}
    assert previews["hit"] == "hello" and previews["recorded"] == "park me please"
    assert previews["miss"].startswith("a miss zzz") and len(previews["miss"]) == 120


async def test_a_timed_out_call_has_a_preview_too(tmp_path: Path) -> None:
    async with make_client(tmp_path, mode="record", record_timeout=0.2) as client:
        resp = await client.post("/v1/chat/completions", json=chat("nobody answers"))
        assert resp.status_code == 504
        (call,) = (await client.get("/api/v1/log")).json()["calls"]
        assert call["outcome"] == "record_timeout" and call["preview"] == "nobody answers"


async def test_the_preview_is_on_the_live_event(replay: httpx.AsyncClient) -> None:
    service = replay._transport.app.state.service  # type: ignore[attr-defined]
    with service.events.subscribe() as sub:
        await replay.post("/v1/chat/completions", json=chat("tell me live"))
        event = await asyncio.wait_for(sub.get(), 2)
    assert event["preview"] == "tell me live"


async def test_a_secret_never_reaches_the_preview_or_the_database(
    replay: httpx.AsyncClient,
) -> None:
    await replay.post("/v1/chat/completions", json=chat(f"deploy with {SECRET_KEY} now"))
    await replay.patch(
        "/api/v1/projects/default", json={"settings": {"redact_patterns": ["ACME-\\d+"]}}
    )
    await replay.post("/v1/chat/completions", json=chat("ticket ACME-4411 broke"))
    calls = (await replay.get("/api/v1/log")).json()["calls"]
    assert [c["preview"] for c in calls] == [
        "ticket [REDACTED] broke",
        "deploy with [REDACTED] now",
    ]
    stored = every_stored_value(replay)
    assert "ant-api03" not in stored and "ACME-4411" not in stored


async def test_old_rows_without_a_preview_read_as_none(replay: httpx.AsyncClient) -> None:
    db = replay._transport.app.state.db  # type: ignore[attr-defined]
    db.run(
        "INSERT INTO call_log (workspace_id, project_id, ts, mode, outcome) "
        "VALUES ('default', 'default', '2026-01-01T00:00:00.000Z', 'replay', 'miss')"
    )
    (call,) = (await replay.get("/api/v1/log")).json()["calls"]
    assert call["preview"] is None


async def test_the_preview_costs_a_hit_no_extra_queries(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    db = replay._transport.app.state.db  # type: ignore[attr-defined]
    statements: list[str] = []

    def count(conn, cursor, statement, *rest):  # type: ignore[no-untyped-def]
        statements.append(statement)

    sa.event.listen(db.engine, "before_cursor_execute", count)
    try:
        await replay.post("/v1/chat/completions", json=chat("hello"))
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", count)
    inserts = [s for s in statements if s.lstrip().upper().startswith("INSERT")]
    assert len(inserts) == 1  # the log row, and nothing else is written
    assert len(statements) <= HIT_STATEMENTS, statements
