from datetime import UTC, timedelta, timezone

import pytest

from semula.dashboard.rows import call_row, clock, keeps, match_text, parse_ts, short, took

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


def test_a_long_prompt_is_cut_for_the_table_but_kept_whole_for_the_detail() -> None:
    long = "word " * 30
    row = call_row({**CALL, "preview": long.strip()}, UTC)
    assert len(row["prompt"]) <= 64 and row["prompt"].endswith("…")
    assert row["preview"] == long.strip()
    assert short("short") == "short" and short("x" * 64) == "x" * 64
    assert short("x" * 65) == "x" * 63 + "…"


def test_a_pending_row_says_where_the_call_sits_in_its_conversation() -> None:
    from semula.dashboard.rows import pending_row

    item = {
        "id": "p",
        "project": "d",
        "created_at": "2026-10-20T10:00:00.000Z",
        "last_message": "x",
    }
    alone = {"id": "01ABCD", "depth": 1, "size": 1, "parent": None}
    third = {"id": "01ABCD", "depth": 3, "size": 5, "parent": "q"}
    assert pending_row({**item, "conversation": alone})["thread"] == ""
    assert pending_row({**item, "conversation": third})["thread"] == "#ABCD · 3 of 5"
    assert pending_row(item)["thread"] == ""


def test_a_parked_call_becomes_a_pending_row() -> None:
    from semula.dashboard.rows import pending_row

    item = {"id": "P1", "project": "triage", "created_at": "2026-10-21T09:14:22.318Z",
            "last_message": "Line one\nLine two"}  # fmt: skip
    row = pending_row(item, UTC)
    assert row == {"id": "P1", "project": "triage", "since": "09:14:22.318",
                   "prompt": "Line one Line two", "thread": ""}  # fmt: skip


def test_typed_answers_are_text_unless_they_start_like_json() -> None:
    from semula.dashboard.rows import answer_from_text

    assert answer_from_text("hello there") == {"content": "hello there"}
    assert answer_from_text(' {"content": "x"} ') == {"content": "x"}
    assert answer_from_text('{"chunks": ["a", "b"]}') == {"chunks": ["a", "b"]}
    for bad in ("", "   ", '{"content": '):
        with pytest.raises(ValueError):
            answer_from_text(bad)


def test_entries_become_rows_with_flags_and_a_page_line() -> None:
    from semula.dashboard.rows import entry_filters, entry_row, page_text

    entry = {"id": "E1", "version": 3, "pinned": True, "hit_count": 12, "responses": 3,
             "selection": "sequence", "last_message": "Classify\nthis", "alias_count": 2,
             "has_tools": True, "updated_at": "2026-10-22T10:03:34.075Z"}  # fmt: skip
    row = entry_row(entry)
    assert row == {"id": "E1", "prompt": "Classify this", "hits": 12, "answers": "3, sequence",
                   "flags": "Pinned · Tools · 2 aliases", "version": "v3",
                   "updated": "2026-10-22 10:03"}  # fmt: skip
    assert entry_row({**entry, "responses": 1, "alias_count": 0, "pinned": False,
                      "has_tools": False})["flags"] == ""  # fmt: skip
    assert entry_filters("Has tool calls") == {"tool_calls": True}
    assert entry_filters("Never hit") == {"never_hit": True} and entry_filters("All") == {}
    assert page_text(123, 50, 50) == "51 to 100 of 123" and page_text(0, 0, 0) == "No entries"


def test_a_revision_becomes_a_history_line() -> None:
    from semula.dashboard.rows import revision_row

    rev = {"version": 2, "ts": "2026-10-22T10:03:31.081Z", "actor": "agent",
           "summary": "answer replaced", "forced": True}  # fmt: skip
    assert revision_row(rev, UTC) == {
        "id": "h2", "version": "v2", "when": "10:03:31.081", "actor": "agent",
        "summary": "answer replaced (forced past a pin)",
    }  # fmt: skip


def test_stored_answers_open_in_the_editor_and_survive_a_round_trip() -> None:
    from semula.dashboard.rows import answers_from_editor, editor_text
    from semula.responses import parse_answer

    plain = parse_answer({"content": "hello"})
    tools = parse_answer({"message": {"content": None, "tool_calls": [
        {"function": {"name": "f", "arguments": {"a": 1}}}]}})  # fmt: skip
    stream = parse_answer({"chunks": ["Hel", "lo"]})

    assert editor_text([plain]) == "hello"
    assert '"arguments": {\n' in editor_text([tools]) and '"a": 1' in editor_text([tools])
    assert '"chunks"' in editor_text([stream])

    for stored in (plain, tools, stream):
        (again,) = answers_from_editor(editor_text([stored]), multiple=False)
        reparsed = parse_answer(again)
        assert reparsed["message"]["content"] == stored["message"]["content"]
        assert reparsed.get("chunks") == stored.get("chunks")
        assert bool(reparsed["message"].get("tool_calls")) == bool(
            stored["message"].get("tool_calls")
        )

    several = answers_from_editor(editor_text([plain, plain]), multiple=True)
    assert several == [{"content": "hello"}, {"content": "hello"}]
    for bad in ("not json", "{}", "[]"):
        with pytest.raises(ValueError):
            answers_from_editor(bad, multiple=True)


def test_the_new_entry_form_becomes_a_chat_request() -> None:
    from semula.dashboard.rows import new_entry_request

    assert new_entry_request("Hi", "", "") == {"messages": [{"role": "user", "content": "Hi"}]}
    full = new_entry_request("Hi", "Be brief", '[{"type": "function"}]')
    assert full["messages"][0] == {"role": "system", "content": "Be brief"}
    assert full["tools"] == [{"type": "function"}]
    for prompt, tools in (("  ", ""), ("Hi", "{"), ("Hi", '{"a": 1}'), ("Hi", "[1]")):
        with pytest.raises(ValueError):
            new_entry_request(prompt, "", tools)


def test_a_candidate_row_and_the_comparison_text() -> None:
    from semula.dashboard.rows import candidate_row, request_text

    candidate = {"id": "C1", "score": 0.9091, "tier": "bm25",
                 "last_message": "my screen\nis badly cracked",
                 "entry_last_message": "my screen is cracked"}  # fmt: skip
    assert candidate_row(candidate) == {
        "id": "C1", "score": "0.91", "tier": "Words",
        "prompt": "my screen is badly cracked", "entry": "my screen is cracked",
    }  # fmt: skip
    text = request_text({"messages": [{"role": "system", "content": "Be brief"},
                                      {"role": "user", "content": "Hi"}],
                         "tools": [{"type": "function", "function": {"name": "f"}}]})  # fmt: skip
    assert text == "system:\nBe brief\n\nuser:\nHi\n\ntools: f"


def test_the_playground_words_a_match_and_a_miss() -> None:
    from semula.dashboard.rows import near_text, verdict_text

    hit = {"matched": True, "tier": "bm25", "score": 0.987, "would": "serve"}
    assert verdict_text(hit) == "Would serve the recorded answer (Words, score 0.99)."
    assert verdict_text({"matched": True, "tier": "exact", "score": 1.0, "would": "serve"}) == (
        "Would serve the recorded answer (Exact)."
    )
    assert verdict_text({"matched": False, "would": "park"}).startswith("Would park")
    assert verdict_text({"matched": False, "would": "no_match_error"}).startswith(
        "Would fail with 424"
    )
    miss = {"matched": False, "nearest": {"last_message": "Name a fruit.", "score": 0.667},
            "candidates": [{"entry_id": "E"}]}  # fmt: skip
    assert near_text(miss) == ('Nearest recorded prompt: "Name a fruit." (score 0.67). '
                               "1 weak candidate(s) would go to the review queue.")  # fmt: skip
    assert near_text({"matched": True, "candidates": []}) == ""


# --- the admin screens (SEM-91) -------------------------------------------------------------
from semula.dashboard.rows import (  # noqa: E402
    audit_row,
    invitation_row,
    member_row,
    stamp,
    token_row,
    usage_lines,
)

TOKEN = {
    "id": "T1", "name": "ci", "scope": "record", "project": None, "prefix": "smla_live_ab",
    "created_at": "2026-10-01T00:00:00.000Z", "expires_at": None, "revoked_at": None,
    "last_used_at": None, "last_used_from": None,
}  # fmt: skip


def test_a_stamp_is_the_date_and_minute_or_nothing() -> None:
    assert stamp("2026-10-22T10:03:44.123Z") == "2026-10-22 10:03"
    assert stamp(None) == "" and stamp("") == ""


def test_a_member_shows_their_name_or_else_their_email() -> None:
    named = {"person_id": "P1", "email": "a@x.io", "name": "Ada", "role": "admin",
             "since": "2026-10-22T10:03:44.123Z"}  # fmt: skip
    assert member_row(named) == {
        "id": "P1", "who": "Ada", "email": "a@x.io", "role": "admin", "since": "2026-10-22 10:03",
    }  # fmt: skip
    assert member_row({**named, "name": None, "since": None})["who"] == "a@x.io"
    assert member_row({**named, "name": "", "since": None})["since"] == ""


def test_an_invitation_row_carries_its_status_and_expiry() -> None:
    row = invitation_row({"id": "I1", "email": "b@x.io", "role": "editor", "status": "open",
                          "expires_at": "2026-10-29T10:03:44.123Z"})  # fmt: skip
    assert row == {"id": "I1", "email": "b@x.io", "role": "editor", "status": "open",
                   "expires": "2026-10-29 10:03"}  # fmt: skip


def test_a_token_row_never_has_more_than_the_prefix_and_names_its_reach() -> None:
    row = token_row(TOKEN)
    assert row["prefix"] == "smla_live_ab…" and row["project"] == "all projects"
    assert row["state"] == "active" and row["used"] == "never"
    bound = token_row({**TOKEN, "project": "shop"})
    assert bound["project"] == "shop"


def test_a_token_is_revoked_or_expired_by_the_clock() -> None:
    assert token_row({**TOKEN, "revoked_at": "2026-10-02T00:00:00.000Z"})["state"] == "revoked"
    assert token_row({**TOKEN, "expires_at": "2020-01-01T00:00:00.000Z"})["state"] == "expired"
    assert token_row({**TOKEN, "expires_at": "2999-01-01T00:00:00.000Z"})["state"] == "active"
    both = {
        **TOKEN,
        "revoked_at": "2026-10-02T00:00:00.000Z",
        "expires_at": "2020-01-01T00:00:00.000Z",
    }
    assert token_row(both)["state"] == "revoked"  # revoking wins over running out


def test_a_token_says_when_and_from_where_it_was_last_used() -> None:
    used = {**TOKEN, "last_used_at": "2026-10-03T08:15:00.000Z"}
    assert token_row(used)["used"] == "2026-10-03 08:15"
    assert (
        token_row({**used, "last_used_from": "10.0.0.7"})["used"]
        == "2026-10-03 08:15 from 10.0.0.7"
    )


def test_an_audit_row_names_the_kind_of_actor_and_the_tail_of_its_id() -> None:
    event = {"id": 12, "ts": "2026-10-22T10:03:44.123Z", "principal_kind": "person",
             "principal_id": "01HZZZZZZZABCDEF", "action": "token.create", "target": "T1",
             "detail": {"scope": "record", "name": "ci"}}  # fmt: skip
    row = audit_row(event)
    assert row["id"] == "12" and row["who"] == "person:ABCDEF" and row["target"] == "T1"
    assert row["detail"] == '{"name": "ci", "scope": "record"}'  # sorted, so it reads the same
    local = audit_row({**event, "principal_kind": "local", "principal_id": "", "target": None,
                       "detail": None})  # fmt: skip
    assert local["who"] == "local" and local["target"] == "" and local["detail"] == ""


def test_usage_lines_show_each_count_against_its_limit() -> None:
    usage = {
        "used": {"projects": 2, "entries": 1500, "bytes": 2_500_000, "parked": 0, "tokens": 3},
        "limits": {"projects": 10, "entries": None, "bytes": 50_000_000,
                   "parked_per_workspace": 100, "tokens": 20},
    }  # fmt: skip
    lines = {line["id"]: line for line in usage_lines(usage)}
    assert lines["projects"]["value"] == "2" and lines["projects"]["limit"] == "10"
    assert lines["entries"]["value"] == "1,500" and lines["entries"]["limit"] == "no limit"
    assert lines["bytes"]["value"] == "2.5 MB" and lines["bytes"]["limit"] == "50.0 MB"
    assert lines["parked"]["limit"] == "100"
    small = usage_lines({"used": {**usage["used"], "bytes": 900}, "limits": {}})
    assert [x for x in small if x["id"] == "bytes"][0]["value"] == "900 B"


def test_the_invitation_link_is_the_public_address_and_the_token() -> None:
    from semula.dashboard.rows import invite_link

    assert invite_link("https://semula.example/", "smla_inv_x") == (
        "https://semula.example/auth/invite/smla_inv_x"
    )


def test_the_audit_filter_offers_every_action_the_service_records() -> None:
    import re
    from pathlib import Path

    from semula.dashboard.rows import AUDIT_ACTIONS

    source = Path("src/semula/service.py").read_text()
    recorded = set(re.findall(r'_audit\(\s*"([a-z_.]+)"', source))
    assert recorded and recorded <= set(AUDIT_ACTIONS), recorded - set(AUDIT_ACTIONS)
