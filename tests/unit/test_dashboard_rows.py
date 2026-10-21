from datetime import UTC, timedelta, timezone

import pytest

from semula.dashboard.rows import call_row, clock, keeps, match_text, parse_ts, took

pytestmark = pytest.mark.unit

CALL = {
    "id": 7, "ts": "2026-10-21T09:14:22.318Z", "project": "triage", "mode": "replay",
    "outcome": "hit", "tier": "bm25", "score": 0.9812, "latency_ms": 4.2, "entry_id": "E1",
    "pending_id": None, "preview": "Classify this ticket",
}  # fmt: skip


def test_a_call_becomes_a_row_with_text_for_every_column() -> None:
    row = call_row(CALL, UTC)
    assert row["id"] == "7" and row["when"] == "09:14:22.318" and row["project"] == "triage"
    assert row["result"] == "Hit" and row["match"] == "Words 0.98" and row["took"] == "4 ms"
    assert row["prompt"] == "Classify this ticket" and row["entry_id"] == "E1"
    assert row["at"] == int(parse_ts(CALL["ts"]).timestamp() * 1000)


def test_the_clock_uses_the_given_zone() -> None:
    moment = parse_ts("2026-10-21T23:59:59.007Z")
    assert clock(moment, UTC) == "23:59:59.007"
    assert clock(moment, timezone(timedelta(hours=8))) == "07:59:59.007"


@pytest.mark.parametrize(
    ("ms", "text"),
    [
        (None, ""),
        (0.4, "0 ms"),
        (4.2, "4 ms"),
        (999, "999 ms"),
        (1500, "1.5 s"),
        (90000, "1.5 min"),
    ],
)
def test_took(ms: float | None, text: str) -> None:
    assert took(ms) == text


@pytest.mark.parametrize(
    ("tier", "score", "text"),
    [
        (None, None, ""),
        ("exact", 1.0, "Exact"),
        ("normalised", 1.0, "Normalised"),
        ("alias", 1.0, "Alias"),
        ("bm25", 0.97, "Words 0.97"),
        ("embedding", 0.8123, "Meaning 0.81"),
        ("bm25", None, "Words"),
        ("odd", None, "odd"),
    ],
)
def test_match_text_shows_a_score_only_when_the_tier_is_not_exact(
    tier: str | None, score: float | None, text: str
) -> None:
    assert match_text(tier, score) == text


def test_older_rows_and_unknown_outcomes_still_make_rows() -> None:
    bare = {"id": 1, "ts": "2026-01-01T00:00:00.000Z", "project": "p", "outcome": "mystery"}
    row = call_row(bare, UTC)
    assert (
        row["prompt"] == ""
        and row["took"] == ""
        and row["match"] == ""
        and row["result"] == "mystery"
    )
    forced = call_row({**bare, "outcome": "forced_write"}, UTC)
    assert forced["result"] == "Forced edit"


def test_filters_by_project_and_chip() -> None:
    hit, miss = call_row(CALL, UTC), call_row({**CALL, "outcome": "miss"}, UTC)
    timed_out = call_row({**CALL, "outcome": "record_timeout"}, UTC)
    recorded = call_row({**CALL, "outcome": "recorded"}, UTC)
    assert all(keeps(r, "triage", "All") for r in (hit, miss, timed_out, recorded))
    assert [keeps(r, None, "Hits") for r in (hit, miss, timed_out, recorded)] == [1, 0, 0, 0]
    assert [keeps(r, None, "Misses") for r in (hit, miss, timed_out, recorded)] == [0, 1, 1, 0]
    assert [keeps(r, None, "Recorded") for r in (hit, miss, timed_out, recorded)] == [0, 0, 0, 1]
    assert not keeps(hit, "other", "All") and keeps(hit, None, "All")


from semula.dashboard.rows import rail_for, rail_from_tests  # noqa: E402


def verdicts(rail: list[dict[str, str]]) -> list[str]:
    return [r["verdict"] for r in rail]


def test_the_rail_for_a_hit_says_no_before_the_answering_tier_and_skipped_after() -> None:
    rail = rail_for({"tier": "bm25", "score": 0.98}, meaning_on=False)
    assert verdicts(rail) == ["no", "no", "no", "match", "off"]
    assert rail[3]["text"] == "Match, 0.98" and rail[4]["text"] == "Off"
    exact = rail_for({"tier": "exact", "score": 1.0}, meaning_on=True)
    assert verdicts(exact) == ["match", "skipped", "skipped", "skipped", "skipped"]
    assert exact[0]["text"] == "Match"
    assert [r["name"] for r in exact] == ["Exact", "Normalised", "Alias", "Words", "Meaning"]


def test_the_rail_when_meaning_answered_and_when_nothing_did() -> None:
    assert verdicts(rail_for({"tier": "embedding", "score": 0.9}, meaning_on=False))[-1] == "match"
    assert verdicts(rail_for({}, meaning_on=True)) == ["no"] * 5
    assert verdicts(rail_for({"tier": None}, meaning_on=False)) == ["no"] * 4 + ["off"]


def test_the_rail_from_a_test_match_answer() -> None:
    rail = rail_from_tests(
        [
            {"tier": "exact", "verdict": "no"},
            {"tier": "normalised", "verdict": "no"},
            {"tier": "alias", "verdict": "no"},
            {"tier": "words", "verdict": "candidate", "score": 0.98, "reason": "in review"},
            {"tier": "meaning", "verdict": "off"},
        ]
    )
    assert [r["text"] for r in rail] == ["No", "No", "No", "0.98, in review", "Off"]
    again = rail_from_tests(
        [{"tier": "words", "verdict": "no", "score": 0.4, "reason": "too different"}]
    )
    assert again[0]["text"] == "No, too different"
