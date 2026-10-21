"""test_match reports a verdict for every tier (SEM-62), for the playground and log detail."""

from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import make_client
from tests.integration.test_embeddings import PARAPHRASE, indexed
from tests.integration.test_embeddings import ask as ask_turns
from tests.integration.test_embeddings import patch as patch_settings
from tests.integration.test_embeddings import seed as seed_turns
from tests.integration.test_lexical_match import BASE, NEAR, UNRELATED, WEAK, ask, relax, seed
from tests.support.embedder import FakeEmbedder

pytestmark = pytest.mark.integration

ORDER = ["exact", "normalised", "alias", "words", "meaning"]


async def tiers(client: httpx.AsyncClient, body: dict) -> dict[str, dict]:
    resp = await client.post("/api/v1/projects/default/test-match", json=body)
    assert resp.status_code == 200, resp.text
    listed = resp.json()["tiers"]
    assert [t["tier"] for t in listed] == ORDER
    return {t["tier"]: t for t in listed}


def verdicts(found: dict[str, dict]) -> list[str]:
    return [found[t]["verdict"] for t in ORDER]


@pytest.fixture
async def semantic(tmp_path: Path) -> httpx.AsyncClient:
    async with make_client(tmp_path, mode="replay", embedder=FakeEmbedder()) as client:
        yield client  # type: ignore[misc]


async def test_an_exact_hit_skips_every_later_tier_and_meaning_is_off(
    replay: httpx.AsyncClient,
) -> None:
    await seed(replay)
    found = await tiers(replay, {"request": ask(BASE)})
    assert verdicts(found) == ["match", "skipped", "skipped", "skipped", "off"]
    assert found["exact"]["score"] == 1.0 and found["exact"]["reason"]
    assert "score" not in found["normalised"]


async def test_a_noisy_prompt_is_a_normalised_match_after_an_exact_no(
    replay: httpx.AsyncClient,
) -> None:
    await seed(replay, "Status at 2026-10-19T10:00:00Z?")
    found = await tiers(replay, {"prompt": "Status at 2026-12-01T09:00:00Z?"})
    assert verdicts(found) == ["no", "match", "skipped", "skipped", "off"]


async def test_an_approved_alias_is_an_alias_match(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    (found,) = (await replay.get("/api/v1/candidates")).json()["candidates"]
    assert (await replay.post(f"/api/v1/candidates/{found['id']}/approve")).status_code == 200
    out = await tiers(replay, {"request": ask(WEAK)})
    assert verdicts(out) == ["no", "no", "match", "skipped", "off"]


async def test_the_words_tier_serves_a_near_prompt_once_the_project_allows_it(
    replay: httpx.AsyncClient,
) -> None:
    await relax(replay)
    await seed(replay)
    out = await tiers(replay, {"request": ask(NEAR)})
    assert verdicts(out) == ["no", "no", "no", "match", "off"]
    assert 0.97 <= out["words"]["score"] < 1


async def test_a_near_prompt_is_a_candidate_by_default(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    out = await tiers(replay, {"request": ask(WEAK)})
    assert verdicts(out) == ["no", "no", "no", "candidate", "off"]
    assert out["words"]["score"] > 0.6 and out["words"]["reason"] == "in review"


async def test_the_words_tier_says_no_when_nothing_is_close(replay: httpx.AsyncClient) -> None:
    empty = await tiers(replay, {"prompt": UNRELATED})
    assert verdicts(empty) == ["no", "no", "no", "no", "off"]
    assert "score" not in empty["words"] and empty["words"]["reason"]
    await seed(replay)
    far = await tiers(replay, {"prompt": UNRELATED})
    assert far["words"]["verdict"] == "no"


async def test_the_words_tier_does_not_offer_a_different_conversation(
    replay: httpx.AsyncClient,
) -> None:
    await seed(replay)
    body = {"messages": [{"role": "system", "content": "x"}, *ask(NEAR)["messages"]]}
    out = await tiers(replay, {"request": body})
    assert out["words"]["verdict"] == "no" and out["words"]["reason"] == "other turns"


async def test_meaning_is_a_candidate_for_a_paraphrase(semantic: httpx.AsyncClient) -> None:
    await seed_turns(semantic)
    indexed(semantic)
    out = await tiers(semantic, {"request": ask_turns(PARAPHRASE)})
    assert verdicts(out) == ["no", "no", "no", "no", "candidate"]
    assert out["meaning"]["score"] >= 0.75 and out["meaning"]["reason"] == "in review"


async def test_meaning_matches_when_the_project_opted_in_to_serving(
    semantic: httpx.AsyncClient,
) -> None:
    await patch_settings(semantic, embedding_serve_threshold=0.8)
    await seed_turns(semantic)
    indexed(semantic)
    out = await tiers(semantic, {"request": ask_turns(PARAPHRASE)})
    assert out["meaning"]["verdict"] == "match" and out["meaning"]["score"] >= 0.8


async def test_meaning_says_no_when_nothing_means_the_same(semantic: httpx.AsyncClient) -> None:
    await seed_turns(semantic)
    indexed(semantic)
    out = await tiers(semantic, {"request": ask_turns("Compile the quarterly tax summary now")})
    assert out["meaning"]["verdict"] == "no"
    empty = await tiers(semantic, {"request": ask_turns("   ")})
    assert empty["meaning"]["verdict"] == "no"
    assert empty["meaning"]["reason"]


async def test_a_failing_model_is_a_no_not_an_error(tmp_path: Path) -> None:
    async with make_client(tmp_path, mode="replay", embedder=FakeEmbedder(fail=True)) as client:
        await seed_turns(client)
        out = await tiers(client, {"request": ask_turns(PARAPHRASE)})
        assert out["meaning"] == {"tier": "meaning", "verdict": "no", "reason": "model failed"}


async def test_the_serving_path_collects_no_trace(replay: httpx.AsyncClient) -> None:
    """Only test_match asks for the trace; a real call passes none."""
    await seed(replay)
    service = replay._transport.app.state.service  # type: ignore[attr-defined]
    seen: list[object] = []
    original = service.lookup

    def spy(req, project="default", trace=None):  # type: ignore[no-untyped-def]
        seen.append(trace)
        return original(req, project, trace)

    service.lookup = spy
    assert (await replay.post("/v1/chat/completions", json=ask(BASE))).status_code == 200
    assert seen == [None]
