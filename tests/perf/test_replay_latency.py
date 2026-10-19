"""Replay latency budgets (SEM-39), on a 1,000-entry project with no embeddings.

* exact and normalised hits, the common case: p95 under 15 ms (the plan's success measure);
* lexical hits (an opt-in tier) and misses, which search the full-text index: p95 under 30 ms.

Runs the real HTTP stack in-process (routing, project lookup, matching, response building,
call logging) against a temp SQLite file, so it measures semula's own overhead, not the
network. ``make bench`` prints the table. Set ``SEMULA_BENCH_BUDGET_MS`` to try another budget.
"""

import os
import random
import statistics
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import make_client

pytestmark = pytest.mark.perf

ENTRIES = 1000
SAMPLES = 300
BUDGET_MS = float(os.environ.get("SEMULA_BENCH_BUDGET_MS", "15"))
SEARCH_BUDGET_MS = BUDGET_MS * 2  # the lexical tier and misses also search the index
WORDS = [f"word{i}" for i in range(4000)]


def prompt(rng: random.Random) -> str:
    """A realistic prompt: a system message's worth of words, ending in a timestamp as many
    frameworks add."""
    return " ".join(rng.choices(WORDS, k=120)) + " generated at 2026-01-01T00:00:00Z"


def unseen(seed: int) -> str:
    return prompt(random.Random(seed + 99))  # noqa: S311 (a benchmark, not security)


def bundle(texts: list[str]) -> dict:
    entries = [
        {
            "id": f"01BENCH{n:019d}",
            "request": {
                "messages": [
                    {"role": "system", "content": "You are a careful assistant."},
                    {"role": "user", "content": text},
                ],
                "tools": [],
                "params": {},
            },
            "responses": [{"message": {"role": "assistant", "content": f"answer {n}"}}],
        }
        for n, text in enumerate(texts)
    ]
    return {"format": "semula/1", "project": {"id": "default"}, "entries": entries, "aliases": []}


def body(text: str) -> dict:
    return {
        "model": "bench",
        "messages": [
            {"role": "system", "content": "You are a careful assistant."},
            {"role": "user", "content": text},
        ],
    }


async def measure(
    client: httpx.AsyncClient, make: Callable[[int], dict], expect: int
) -> list[float]:
    times: list[float] = []
    for i in range(SAMPLES):
        payload = make(i)
        started = time.perf_counter()
        resp = await client.post("/v1/chat/completions", json=payload)
        times.append((time.perf_counter() - started) * 1000)
        assert resp.status_code == expect, resp.text
    return times


def p95(times: list[float]) -> float:
    return sorted(times)[int(len(times) * 0.95) - 1]


async def test_replay_hits_stay_inside_the_latency_budget(tmp_path: Path) -> None:
    rng = random.Random(7)  # noqa: S311 (a benchmark, not security)
    texts = [prompt(rng) for _ in range(ENTRIES)]
    async with make_client(tmp_path, mode="replay") as client:
        loaded = await client.post("/api/v1/import", json={"bundle": bundle(texts)})
        assert loaded.json()["entries"]["created"] == ENTRIES
        await client.patch(
            "/api/v1/projects/default", json={"settings": {"bm25_serve_threshold": 0.97}}
        )
        pick = [rng.randrange(ENTRIES) for _ in range(SAMPLES)]

        await measure(client, lambda i: body(texts[pick[i]]), 200)  # warm up

        results = {
            "exact hit": await measure(client, lambda i: body(texts[pick[i]]), 200),
            "normalised hit": await measure(
                client,
                lambda i: body(texts[pick[i]].replace("2026-01-01", f"2026-10-{i % 28 + 1:02d}")),
                200,
            ),
            "lexical hit": await measure(client, lambda i: body(f"{texts[pick[i]]} please"), 200),
            "miss": await measure(client, lambda i: body(unseen(i)), 424),
        }
    budgets = {
        "exact hit": BUDGET_MS,
        "normalised hit": BUDGET_MS,
        "lexical hit": SEARCH_BUDGET_MS,
        "miss": SEARCH_BUDGET_MS,
    }
    print(f"\n{ENTRIES} entries, {SAMPLES} samples each")
    for name, times in results.items():
        print(
            f"  {name:<15} p50 {statistics.median(times):6.2f} ms   p95 {p95(times):6.2f} ms"
            f"   max {max(times):6.2f} ms   (budget p95 < {budgets[name]:g} ms)"
        )
    over = {name: round(p95(t), 2) for name, t in results.items() if p95(t) >= budgets[name]}
    assert not over, f"p95 over budget (ms): {over}"
