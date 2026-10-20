"""The real model, end to end (SEM-E8). Needs the `embeddings` extra and, the first time, network.

Kept out of the fast layer: `make test-embeddings`, and its own CI job with the model cached.
Set ``SEMULA_TEST_MODEL_DIR`` to reuse a downloaded model between runs.
"""

import os
import random
import statistics
import time
from pathlib import Path

import pytest

from semula.embeddings import MODEL_ID, EmbeddingsUnavailable, FastEmbedder
from tests.integration.conftest import make_client

pytestmark = pytest.mark.embeddings

BUDGET_MS = float(os.environ.get("SEMULA_EMBED_BUDGET_MS", "55"))


@pytest.fixture(scope="session")
def model_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = Path(os.environ.get("SEMULA_TEST_MODEL_DIR") or tmp_path_factory.mktemp("models"))
    FastEmbedder(directory)  # downloads once when missing
    return directory


def ask(text: str) -> dict:
    return {"model": "m", "messages": [{"role": "user", "content": text}]}


def test_the_model_loads_offline_once_it_is_downloaded(
    model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    embedder = FastEmbedder(model_dir, offline=True)
    assert embedder.model_id == MODEL_ID
    vectors = embedder.embed(["a sentence", "another sentence"])
    assert vectors.shape == (2, 384)
    assert abs(float((vectors[0] ** 2).sum()) - 1.0) < 1e-4  # unit length


def test_offline_without_a_download_fails_with_a_clear_message(tmp_path: Path) -> None:
    with pytest.raises(EmbeddingsUnavailable, match="semula embeddings fetch"):
        FastEmbedder(tmp_path / "empty", offline=True)


def test_a_paraphrase_scores_above_an_unrelated_sentence(model_dir: Path) -> None:
    embedder = FastEmbedder(model_dir, offline=True)
    a, b, c = embedder.embed(
        [
            "Order A-1042 arrived with a cracked screen, please refund me",
            "My package came and the display is broken, I want my money back",
            "What is the capital of France?",
        ]
    )
    assert float(a @ b) > 0.7 > float(a @ c)


async def test_a_real_paraphrase_reaches_review_and_never_serves(
    tmp_path: Path, model_dir: Path
) -> None:
    async with make_client(
        tmp_path, mode="replay", embeddings=True, embeddings_offline=True, model_dir=model_dir
    ) as client:
        service = client._transport.app.state.service  # type: ignore[attr-defined]
        await client.post(
            "/api/v1/projects/default/entries",
            json={
                "request": ask("Order A-1042 arrived with a cracked screen, please refund me"),
                "response": {"content": "Refund issued."},
            },
        )
        assert service.indexer.flush(60)
        resp = await client.post(
            "/v1/chat/completions",
            json=ask("My package came and the display is broken, I want my money back"),
        )
        assert resp.status_code == 424
        (found,) = (await client.get("/api/v1/candidates")).json()["candidates"]
        assert found["tier"] == "embedding" and found["score"] >= 0.75
        unrelated = await client.post("/v1/chat/completions", json=ask("What is the capital?"))
        assert unrelated.status_code == 424
        assert len((await client.get("/api/v1/candidates")).json()["candidates"]) == 1


async def test_a_miss_with_embeddings_on_stays_inside_the_budget(
    tmp_path: Path, model_dir: Path
) -> None:
    rng = random.Random(3)  # noqa: S311 (a benchmark, not security)
    words = [f"term{i}" for i in range(3000)]
    async with make_client(
        tmp_path, mode="replay", embeddings=True, embeddings_offline=True, model_dir=model_dir
    ) as client:
        service = client._transport.app.state.service  # type: ignore[attr-defined]
        entries = [
            {
                "id": f"01EMBED{n:019d}",
                "request": {
                    "messages": [{"role": "user", "content": " ".join(rng.choices(words, k=25))}],
                    "tools": [],
                    "params": {},
                },
                "responses": [{"message": {"role": "assistant", "content": f"a{n}"}}],
            }
            for n in range(1000)
        ]
        bundle = {"format": "semula/1", "project": {"id": "default"}, "entries": entries}
        assert (await client.post("/api/v1/import", json={"bundle": bundle})).status_code == 200
        assert service.indexer.flush(600), "indexing 1,000 entries took too long"
        status = (await client.get("/api/v1/embeddings")).json()
        assert status["indexed"] == 1000

        times: list[float] = []
        for i in range(60):
            text = " ".join(random.Random(10_000 + i).choices(words, k=25))  # noqa: S311
            started = time.perf_counter()
            resp = await client.post("/v1/chat/completions", json=ask(text))
            times.append((time.perf_counter() - started) * 1000)
            assert resp.status_code == 424
    times.sort()
    p95 = times[int(len(times) * 0.95) - 1]
    print(f"\nmiss with embeddings, 1,000 entries: p50 {statistics.median(times):.1f} ms, "
          f"p95 {p95:.1f} ms (budget {BUDGET_MS:g} ms)")  # fmt: skip
    assert p95 < BUDGET_MS, f"p95 {p95:.1f} ms is over the {BUDGET_MS:g} ms budget"
