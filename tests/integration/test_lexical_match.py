"""The lexical (BM25) tier and the review queue (SEM-31 to SEM-33, ADR-0003)."""

import asyncio
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import make_client
from tests.integration.test_replay import record_answer
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.integration

BASE = (
    "You are a careful code reviewer. Read the function below and list every bug you can "
    "find, one per line, with the line number and a short fix. Do not rewrite the function. "
    "def add(a, b): return a - b"
)
NEAR = BASE + " Thanks."  # one extra word: ~0.99 overlap
WEAK = (  # changed code and a different task wording: overlap between the two thresholds
    "You are a careful code reviewer. Read the function below and list every issue you can "
    "find, one per line, with the line number and a short fix. Do not rewrite the function. "
    "def mul(x, y): return x + y"
)
OTHER_WEAK = WEAK.replace("def mul(x, y): return x + y", "def div(p, q): return p // q")
UNRELATED = "Translate the following sentence into French: the weather is lovely today."


def ask(text: str, **extra: object) -> dict:
    return {"model": "m", "messages": [{"role": "user", "content": text}], **extra}


async def seed(client: httpx.AsyncClient, text: str = BASE, answer: str = "line 1: use +") -> str:
    resp = await client.post(
        "/api/v1/projects/default/entries",
        json={"request": ask(text), "response": {"content": answer}},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def settings(client: httpx.AsyncClient, **values: object) -> httpx.Response:
    return await client.patch("/api/v1/projects/default", json={"settings": values})


async def relax(client: httpx.AsyncClient) -> None:
    """Opt in to auto-serving near-identical prompts; the default sends them to review."""
    assert (await settings(client, bm25_serve_threshold=0.97)).status_code == 200


async def candidates(client: httpx.AsyncClient, status: str = "open") -> list[dict]:
    resp = await client.get("/api/v1/candidates", params={"status": status})
    assert resp.status_code == 200, resp.text
    return resp.json()["candidates"]  # type: ignore[no-any-return]


def last_call(client: httpx.AsyncClient) -> dict:
    db = client._transport.app.state.db  # type: ignore[attr-defined]
    row = db.query_one("SELECT outcome, tier, score FROM call_log ORDER BY id DESC LIMIT 1")
    return dict(row)  # type: ignore[arg-type]


# --- SEM-31: the BM25 tier ---------------------------------------------------------------
async def test_a_near_identical_prompt_hits_above_the_threshold(replay: httpx.AsyncClient) -> None:
    await relax(replay)
    await seed(replay)
    resp = await replay.post("/v1/chat/completions", json=ask(NEAR))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "line 1: use +"
    call = last_call(replay)
    assert (call["outcome"], call["tier"]) == ("hit", "bm25")
    assert 0.97 <= call["score"] < 1.0


async def test_below_the_threshold_it_falls_through_to_a_miss(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    resp = await replay.post("/v1/chat/completions", json=ask(WEAK))
    assert resp.status_code == 424
    assert resp.json()["error"]["code"] == "semula_no_match"
    assert last_call(replay)["outcome"] == "miss"


async def test_by_default_a_near_identical_prompt_is_reviewed_not_served(
    replay: httpx.AsyncClient,
) -> None:
    await seed(replay)
    assert (await replay.post("/v1/chat/completions", json=ask(NEAR))).status_code == 424
    (found,) = await candidates(replay)
    assert found["score"] >= 0.97


async def test_the_threshold_is_per_project(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    assert (await replay.post("/v1/chat/completions", json=ask(WEAK))).status_code == 424
    assert (await settings(replay, bm25_serve_threshold=0.7)).status_code == 200
    resp = await replay.post("/v1/chat/completions", json=ask(WEAK))
    assert resp.status_code == 200 and last_call(replay)["tier"] == "bm25"
    assert (await settings(replay, bm25_serve_threshold=0.999)).status_code == 200
    assert (await replay.post("/v1/chat/completions", json=ask(NEAR))).status_code == 424


async def test_review_cannot_be_set_above_serve(replay: httpx.AsyncClient) -> None:
    resp = await settings(replay, bm25_serve_threshold=0.5)  # default review floor is 0.6
    assert resp.status_code == 422
    assert resp.json()["error"]["param"] == "settings"
    assert "cannot exceed" in resp.json()["error"]["message"]
    both = await settings(replay, bm25_serve_threshold=0.5, bm25_review_threshold=0.4)
    assert both.status_code == 200


async def test_the_same_words_with_other_tools_are_not_auto_served(
    replay: httpx.AsyncClient,
) -> None:
    await seed(replay)
    tool = {
        "type": "function",
        "function": {"name": "flag", "parameters": {"type": "object", "properties": {}}},
    }
    resp = await replay.post("/v1/chat/completions", json=ask(NEAR, tools=[tool]))
    assert resp.status_code == 424  # a recorded answer might call a tool this request lacks
    assert [c["score"] >= 0.97 for c in await candidates(replay)] == [True]  # reviewed instead


async def test_params_a_project_requires_to_match_also_gate_the_lexical_tier(
    replay: httpx.AsyncClient,
) -> None:
    await relax(replay)
    await settings(replay, include_params=["temperature"])
    await replay.post(
        "/api/v1/projects/default/entries",
        json={"request": ask(BASE, temperature=0.1), "response": {"content": "x"}},
    )
    assert (
        await replay.post("/v1/chat/completions", json=ask(NEAR, temperature=0.9))
    ).status_code == 424
    assert (
        await replay.post("/v1/chat/completions", json=ask(NEAR, temperature=0.1))
    ).status_code == 200


async def test_a_deleted_entry_leaves_the_index(replay: httpx.AsyncClient) -> None:
    await relax(replay)
    entry = (await replay.get("/api/v1/entries/" + await seed(replay))).json()
    assert (await replay.post("/v1/chat/completions", json=ask(NEAR))).status_code == 200
    await replay.delete(f"/api/v1/entries/{entry['id']}", params={"version": entry["version"]})
    assert (await replay.post("/v1/chat/completions", json=ask(NEAR))).status_code == 424
    db = replay._transport.app.state.db  # type: ignore[attr-defined]
    if db.dialect == "sqlite":  # the stale row would be harmless, but it must not pile up
        assert db.query("SELECT 1 FROM entry_fts") == []


async def test_projects_do_not_see_each_others_entries(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    await replay.post("/api/v1/projects", json={"id": "other"})
    resp = await replay.post("/v1/projects/other/chat/completions", json=ask(NEAR))
    assert resp.status_code == 424


async def test_changing_a_scrub_rule_reindexes_the_words(replay: httpx.AsyncClient) -> None:
    # The only word the two prompts share, once the rule hides the ticket id, is the placeholder
    # the scrubber writes: found only if the index was rebuilt with the new rule.
    await seed(replay, "ticket-12")
    await settings(replay, bm25_serve_threshold=0.6, bm25_review_threshold=0.5)
    assert (await replay.post("/v1/chat/completions", json=ask("ticket-99 now"))).status_code == 424
    await settings(replay, scrub_patterns=[r"ticket-\d+"])
    resp = await replay.post("/v1/chat/completions", json=ask("ticket-99 now"))
    assert resp.status_code == 200 and last_call(replay)["tier"] == "bm25"


async def test_an_index_that_fell_behind_is_repaired_at_startup(tmp_path: Path) -> None:
    url = fresh_database_url(tmp_path)
    async with make_client(tmp_path, mode="replay", database_url=url) as first:
        await relax(first)
        await seed(first)
        db = first._transport.app.state.db  # type: ignore[attr-defined]
        if db.dialect != "sqlite":
            pytest.skip("only the SQLite FTS index keeps state")
        db.run("DELETE FROM entry_fts")  # as after a restore, or a migration on old data
        assert (await first.post("/v1/chat/completions", json=ask(NEAR))).status_code == 424
    async with make_client(tmp_path, mode="replay", database_url=url) as second:
        assert (await second.post("/v1/chat/completions", json=ask(NEAR))).status_code == 200


async def test_test_match_reports_the_tier_and_score_and_changes_nothing(
    replay: httpx.AsyncClient,
) -> None:
    await relax(replay)
    await seed(replay)
    out = (await replay.post("/api/v1/projects/default/test-match", json={"prompt": NEAR})).json()
    assert out["tier"] == "bm25" and out["would"] == "serve" and 0.97 <= out["score"] < 1
    weak = (await replay.post("/api/v1/projects/default/test-match", json={"prompt": WEAK})).json()
    assert weak["matched"] is False and weak["candidates"][0]["score"] > 0.6
    assert await candidates(replay) == []  # asking never queues anything
    db = replay._transport.app.state.db  # type: ignore[attr-defined]
    assert db.query_one("SELECT COUNT(*) AS n FROM call_log")["n"] == 0


# --- SEM-32: the review queue ------------------------------------------------------------
async def test_a_weak_candidate_appears_with_its_score_and_is_not_served(
    replay: httpx.AsyncClient,
) -> None:
    entry_id = await seed(replay)
    resp = await replay.post("/v1/chat/completions", json=ask(WEAK))
    assert resp.status_code == 424  # the call is a miss
    (found,) = await candidates(replay)
    assert found["entry_id"] == entry_id and found["status"] == "open" and found["tier"] == "bm25"
    assert 0.6 <= found["score"] < 0.97
    assert found["last_message"].endswith("return x + y")
    assert found["entry_last_message"].endswith("return a - b")
    full = (await replay.get(f"/api/v1/candidates/{found['id']}")).json()
    assert full["request"]["messages"][0]["content"] == WEAK
    assert full["entry"]["id"] == entry_id


async def test_an_unrelated_prompt_queues_nothing(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    await replay.post("/v1/chat/completions", json=ask(UNRELATED))
    assert await candidates(replay) == []


async def test_the_same_miss_twice_is_one_candidate(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    for _ in range(3):
        await replay.post("/v1/chat/completions", json=ask(WEAK))
    assert len(await candidates(replay)) == 1


async def test_in_record_mode_a_weak_candidate_still_parks_the_call(
    record: httpx.AsyncClient,
) -> None:
    await seed(record)
    call = asyncio.create_task(record.post("/v1/chat/completions", json=ask(WEAK)))
    for _ in range(100):
        pending = (await record.get("/api/v1/pending")).json()["pending"]
        if pending:
            break
        await asyncio.sleep(0.01)
    assert pending and not call.done()
    assert len(await candidates(record)) == 1
    answered = await record.post(
        f"/api/v1/pending/{pending[0]['id']}/answer", json={"content": "fresh"}
    )
    assert answered.status_code == 200
    assert (await call).json()["choices"][0]["message"]["content"] == "fresh"
    # the request now has an answer of its own, so the near-miss is moot
    assert await candidates(record) == []
    (closed,) = await candidates(record, "superseded")
    assert closed["status"] == "superseded"


async def test_deleting_the_entry_removes_its_candidates(replay: httpx.AsyncClient) -> None:
    entry = (await replay.get("/api/v1/entries/" + await seed(replay))).json()
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    await replay.delete(f"/api/v1/entries/{entry['id']}", params={"version": entry["version"]})
    assert await candidates(replay, "all") == []


async def test_candidates_can_be_listed_per_project(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    await replay.post("/api/v1/projects", json={"id": "other"})
    mine = await replay.get("/api/v1/candidates", params={"project": "other"})
    assert mine.json() == {"candidates": []}
    assert (await replay.get("/api/v1/candidates", params={"project": "nope"})).status_code == 404


# --- SEM-33: approve or reject -----------------------------------------------------------
async def test_an_approved_prompt_hits_next_time(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    (found,) = await candidates(replay)
    done = await replay.post(f"/api/v1/candidates/{found['id']}/approve")
    assert done.status_code == 200 and done.json()["status"] == "approved"
    resp = await replay.post("/v1/chat/completions", json=ask(WEAK))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "line 1: use +"
    assert last_call(replay)["tier"] == "alias"
    assert await candidates(replay) == []


async def test_an_alias_follows_its_entrys_later_edits(replay: httpx.AsyncClient) -> None:
    entry_id = await seed(replay)
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    (found,) = await candidates(replay)
    await replay.post(f"/api/v1/candidates/{found['id']}/approve")
    entry = (await replay.get(f"/api/v1/entries/{entry_id}")).json()
    await replay.put(
        f"/api/v1/entries/{entry_id}",
        json={"version": entry["version"], "response": {"content": "edited"}},
    )
    resp = await replay.post("/v1/chat/completions", json=ask(WEAK))
    assert resp.json()["choices"][0]["message"]["content"] == "edited"


async def test_an_alias_survives_a_matching_setting_change(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    (found,) = await candidates(replay)
    await replay.post(f"/api/v1/candidates/{found['id']}/approve")
    await settings(replay, scrub_patterns=["zzz"])  # re-keys entries, pending and aliases
    assert (await replay.post("/v1/chat/completions", json=ask(WEAK))).status_code == 200


async def test_a_rejected_pair_never_reappears(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    (found,) = await candidates(replay)
    done = await replay.post(f"/api/v1/candidates/{found['id']}/reject")
    assert done.status_code == 200 and done.json()["status"] == "rejected"
    for _ in range(2):
        assert (await replay.post("/v1/chat/completions", json=ask(WEAK))).status_code == 424
    assert await candidates(replay) == []
    (only,) = await candidates(replay, "all")
    assert only["id"] == found["id"] and only["status"] == "rejected"
    # a rejection also stops it being served, even when the threshold would allow it
    await settings(replay, bm25_serve_threshold=0.7)
    assert (await replay.post("/v1/chat/completions", json=ask(WEAK))).status_code == 424


async def test_a_rejection_does_not_hide_other_entries_for_the_same_prompt(
    replay: httpx.AsyncClient,
) -> None:
    await seed(replay)
    other = await seed(replay, OTHER_WEAK, "second")
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    found = await candidates(replay)
    assert len(found) == 2 and found[0]["score"] >= found[1]["score"]
    await replay.post(f"/api/v1/candidates/{found[0]['id']}/reject")
    (left,) = await candidates(replay)
    assert left["entry_id"] != found[0]["entry_id"] and other in {
        left["entry_id"],
        found[0]["entry_id"],
    }


async def test_approving_closes_the_other_candidates_for_that_prompt(
    replay: httpx.AsyncClient,
) -> None:
    await seed(replay)
    await seed(replay, OTHER_WEAK, "second")
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    found = await candidates(replay)
    assert len(found) == 2
    await replay.post(f"/api/v1/candidates/{found[0]['id']}/approve")
    assert await candidates(replay) == []
    assert [c["status"] for c in await candidates(replay, "superseded")] == ["superseded"]


async def test_a_decision_is_final_and_unknown_ids_are_404(replay: httpx.AsyncClient) -> None:
    await seed(replay)
    await replay.post("/v1/chat/completions", json=ask(WEAK))
    (found,) = await candidates(replay)
    assert (await replay.post(f"/api/v1/candidates/{found['id']}/approve")).status_code == 200
    for verb in ("approve", "reject"):
        again = await replay.post(f"/api/v1/candidates/{found['id']}/{verb}")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "candidate_decided"
    missing = await replay.post("/api/v1/candidates/01NOPE/approve")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "candidate_not_found"


async def test_recording_an_answer_through_the_queue_still_works_with_candidates_around(
    record: httpx.AsyncClient,
) -> None:
    await seed(record)
    first = await record_answer(record, ask(UNRELATED), {"content": "Bonjour"})
    assert first["choices"][0]["message"]["content"] == "Bonjour"


async def test_only_conversations_with_the_same_turns_are_queued_for_review(
    replay: httpx.AsyncClient,
) -> None:
    """Found driving the tool end to end: every call in a project shares its system prompt, so
    a one-message prompt looked 80-90% like each longer conversation that contains it. Those
    are never the same question, and they buried the one pairing worth reviewing."""
    system = {"role": "system", "content": "You are a careful code reviewer. Reply tersely."}

    def convo(*turns: tuple[str, str]) -> dict:
        return {"messages": [system, *({"role": r, "content": c} for r, c in turns)]}

    first = convo(("user", BASE))
    longer = convo(("user", BASE), ("assistant", "ok"), ("user", "now fix it"))
    for request, answer in ((first, "one"), (longer, "two")):
        resp = await replay.post(
            "/api/v1/projects/default/entries",
            json={"request": request, "response": {"content": answer}},
        )
        assert resp.status_code == 201, resp.text
    await replay.post("/v1/chat/completions", json=convo(("user", WEAK)))
    found = await candidates(replay)
    assert [c["entry_id"] for c in found] == [
        (await replay.get("/api/v1/projects/default/entries")).json()["entries"][0]["id"]
    ]
    assert len(found) == 1
