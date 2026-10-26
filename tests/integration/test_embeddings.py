"""The optional embedding tier (SEM-E8, ADR-0003), against a fake model."""

import asyncio
import threading
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import make_client
from tests.support.db import fresh_database_url
from tests.support.embedder import FakeEmbedder

pytestmark = pytest.mark.integration

ORIGINAL = "Order A-1042 arrived and the screen is cracked please refund me today"
PARAPHRASE = "Package 77 delivered but the monitor is shattered kindly reimburse me today"
# the paraphrase shares almost no words with the original, so only the embedding tier sees it


def ask(text: str, *turns: tuple[str, str]) -> dict:
    messages = [{"role": "system", "content": "Be brief."}]
    messages += [{"role": r, "content": c} for r, c in turns]
    messages.append({"role": "user", "content": text})
    return {"model": "m", "messages": messages}


async def seed(client: httpx.AsyncClient, text: str = ORIGINAL, answer: str = "refunded") -> str:
    resp = await client.post(
        "/api/v1/projects/default/entries",
        json={"request": ask(text), "response": {"content": answer}},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def indexed(client: httpx.AsyncClient) -> None:
    """Wait for the background indexer to make every queued vector."""
    service = client._transport.app.state.service  # type: ignore[attr-defined]
    assert service.indexer.flush(10), "the indexer never caught up"


async def candidates(client: httpx.AsyncClient, status: str = "open") -> list[dict]:
    resp = await client.get("/api/v1/candidates", params={"status": status})
    return resp.json()["candidates"]  # type: ignore[no-any-return]


async def patch(client: httpx.AsyncClient, **values: object) -> httpx.Response:
    return await client.patch("/api/v1/projects/default", json={"settings": values})


@pytest.fixture
async def semantic(tmp_path: Path) -> httpx.AsyncClient:
    async with make_client(tmp_path, mode="replay", embedder=FakeEmbedder()) as client:
        yield client  # type: ignore[misc]


async def test_by_default_embeddings_are_off_and_nothing_is_embedded(
    replay: httpx.AsyncClient,
) -> None:
    assert (await replay.get("/api/v1/embeddings")).json() == {"enabled": False}
    await seed(replay)
    resp = await replay.post("/v1/chat/completions", json=ask(PARAPHRASE))
    assert resp.status_code == 424
    assert await candidates(replay) == []
    service = replay._transport.app.state.service  # type: ignore[attr-defined]
    assert service.vectors is None and service.indexer is None


async def test_a_paraphrase_appears_in_review_with_a_score_and_is_not_served(
    semantic: httpx.AsyncClient,
) -> None:
    entry_id = await seed(semantic)
    indexed(semantic)
    resp = await semantic.post("/v1/chat/completions", json=ask(PARAPHRASE))
    assert resp.status_code == 424  # a paraphrase is never served by default
    (found,) = await candidates(semantic)
    assert found["entry_id"] == entry_id and found["tier"] == "embedding"
    assert 0.75 <= found["score"] <= 1.0
    detail = (await semantic.get(f"/api/v1/candidates/{found['id']}")).json()
    assert detail["request"]["messages"][-1]["content"] == PARAPHRASE


async def test_approving_a_paraphrase_serves_it_next_time(semantic: httpx.AsyncClient) -> None:
    await seed(semantic)
    indexed(semantic)
    await semantic.post("/v1/chat/completions", json=ask(PARAPHRASE))
    (found,) = await candidates(semantic)
    await semantic.post(f"/api/v1/candidates/{found['id']}/approve")
    hit = await semantic.post("/v1/chat/completions", json=ask(PARAPHRASE))
    assert hit.status_code == 200 and hit.json()["choices"][0]["message"]["content"] == "refunded"


async def test_a_rejected_paraphrase_never_reappears(semantic: httpx.AsyncClient) -> None:
    await seed(semantic)
    indexed(semantic)
    await semantic.post("/v1/chat/completions", json=ask(PARAPHRASE))
    (found,) = await candidates(semantic)
    await semantic.post(f"/api/v1/candidates/{found['id']}/reject")
    await semantic.post("/v1/chat/completions", json=ask(PARAPHRASE))
    assert await candidates(semantic) == []
    assert [c["status"] for c in await candidates(semantic, "all")] == ["rejected"]


async def test_an_unrelated_prompt_is_not_queued(semantic: httpx.AsyncClient) -> None:
    await seed(semantic)
    indexed(semantic)
    await semantic.post("/v1/chat/completions", json=ask("What is the capital of France?"))
    assert await candidates(semantic) == []


async def test_only_conversations_with_the_same_turns_are_candidates(
    semantic: httpx.AsyncClient,
) -> None:
    longer = ask(ORIGINAL, ("user", "hello"), ("assistant", "hi"))
    await semantic.post(
        "/api/v1/projects/default/entries", json={"request": longer, "response": {"content": "x"}}
    )
    indexed(semantic)
    await semantic.post("/v1/chat/completions", json=ask(PARAPHRASE))  # a one-turn conversation
    assert await candidates(semantic) == []


# --- SEM-42: the opt-in threshold ----------------------------------------------------------
async def test_the_default_never_auto_serves_even_an_identical_vector(
    semantic: httpx.AsyncClient,
) -> None:
    await seed(semantic)
    indexed(semantic)
    settings = (await semantic.get("/api/v1/projects/default")).json()["settings"]
    assert settings["embedding_serve_threshold"] is None
    resp = await semantic.post("/v1/chat/completions", json=ask(ORIGINAL.replace("A-1042", "B-7")))
    assert resp.status_code == 424


async def test_an_opted_in_threshold_serves_and_the_call_log_says_so(
    semantic: httpx.AsyncClient,
) -> None:
    await seed(semantic)
    indexed(semantic)
    assert (await patch(semantic, embedding_serve_threshold=0.8)).status_code == 200
    resp = await semantic.post("/v1/chat/completions", json=ask(PARAPHRASE))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "refunded"
    (call,) = (await semantic.get("/api/v1/log", params={"outcome": "hit"})).json()["calls"]
    assert call["tier"] == "embedding" and 0.8 <= call["score"] <= 1.0
    assert await candidates(semantic) == []  # served, so nothing to review


async def test_an_opted_in_threshold_still_needs_the_same_conversation_shape(
    semantic: httpx.AsyncClient,
) -> None:
    tool = {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}
    await seed(semantic)
    indexed(semantic)
    await patch(semantic, embedding_serve_threshold=0.8)
    resp = await semantic.post("/v1/chat/completions", json={**ask(PARAPHRASE), "tools": [tool]})
    assert resp.status_code == 424  # an answer recorded without that tool is not safe to serve
    assert len(await candidates(semantic)) == 1


@pytest.mark.parametrize(
    ("values", "ok"),
    [
        ({"embedding_serve_threshold": 0.9}, True),
        ({"embedding_serve_threshold": 0.5}, False),  # below the 0.75 review floor
        ({"embedding_review_threshold": 1.5}, False),
        ({"embedding_serve_threshold": None}, True),  # null resets to off
    ],
)
async def test_the_embedding_thresholds_are_validated(
    semantic: httpx.AsyncClient, values: dict, ok: bool
) -> None:
    assert ((await patch(semantic, **values)).status_code == 200) is ok


# --- indexing and robustness ----------------------------------------------------------------
async def test_vectors_follow_entries_through_create_import_and_delete(
    semantic: httpx.AsyncClient,
) -> None:
    first = await seed(semantic)
    indexed(semantic)
    assert (await semantic.get("/api/v1/embeddings")).json() == {
        "enabled": True, "model": "fake-synonyms", "entries": 1, "indexed": 1, "backlog": 0,
    }  # fmt: skip
    bundle = (await semantic.get("/api/v1/projects/default/export")).json()
    entry = (await semantic.get(f"/api/v1/entries/{first}")).json()
    await semantic.delete(f"/api/v1/entries/{first}", params={"version": entry["version"]})
    indexed(semantic)
    gone = (await semantic.get("/api/v1/embeddings")).json()
    assert (gone["entries"], gone["indexed"]) == (0, 0)  # the vector went with its entry
    assert (await semantic.post("/api/v1/import", json={"bundle": bundle})).status_code == 200
    indexed(semantic)
    back = (await semantic.get("/api/v1/embeddings")).json()
    assert (back["entries"], back["indexed"]) == (1, 1)  # an import is indexed too


async def test_an_existing_database_is_backfilled_at_startup(tmp_path: Path) -> None:
    url = fresh_database_url(tmp_path)
    async with make_client(tmp_path, mode="replay", database_url=url) as plain:  # no embeddings
        await seed(plain)
        await seed(plain, "Another entry about invoices and billing questions")
    async with make_client(
        tmp_path, mode="replay", database_url=url, embedder=FakeEmbedder()
    ) as semantic:
        indexed(semantic)
        status = (await semantic.get("/api/v1/embeddings")).json()
        assert (status["entries"], status["indexed"]) == (2, 2)


async def test_writing_an_entry_does_not_wait_for_the_model(tmp_path: Path) -> None:
    # No clock: the model is stuck until the test lets it go, and the write must finish anyway.
    # (A wall-clock limit on a write failed on a slow CI runner.)
    gate = threading.Event()
    stuck = FakeEmbedder(gate=gate)
    async with make_client(tmp_path, mode="replay", embedder=stuck) as client:
        try:
            await asyncio.wait_for(seed(client), 10)  # a write that waited on the model hangs here
            assert stuck.seen == []  # and the model had still not produced anything
        finally:
            gate.set()
        indexed(client)  # released, the vector arrives in the background
        assert stuck.seen


async def test_hits_never_touch_the_model(tmp_path: Path) -> None:
    fake = FakeEmbedder()
    async with make_client(tmp_path, mode="replay", embedder=fake) as client:
        await seed(client)
        indexed(client)
        before = len(fake.seen)
        for _ in range(3):
            assert (
                await client.post("/v1/chat/completions", json=ask(ORIGINAL))
            ).status_code == 200
        assert len(fake.seen) == before


async def test_a_failing_model_does_not_break_the_call(tmp_path: Path) -> None:
    async with make_client(tmp_path, mode="replay", embedder=FakeEmbedder(fail=True)) as client:
        await seed(client)
        resp = await client.post("/v1/chat/completions", json=ask(PARAPHRASE))
        assert resp.status_code == 424
        assert resp.json()["error"]["code"] == "semula_no_match"  # a normal miss, not a 500


async def test_the_model_only_ever_sees_redacted_text(tmp_path: Path) -> None:
    fake = FakeEmbedder()
    key = "sk-" + "ant-api03-AbCdEfGhIjKlMnOpQrStUvWx"
    async with make_client(tmp_path, mode="replay", embedder=fake) as client:
        await seed(client, f"please use {key} for the refund")
        indexed(client)
        await client.post("/v1/chat/completions", json=ask(f"a different request with {key}"))
        assert fake.seen and all("ant-api03" not in text for text in fake.seen)
