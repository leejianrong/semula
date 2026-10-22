from pathlib import Path

import pytest

from semula.canonical import FingerprintConfig, from_openai, lexical_tokens, shape
from semula.db import Database
from semula.lexical import Fts5Index, ScanIndex, make_index, similarity
from tests.support.db import fresh_database_url

pytestmark = pytest.mark.unit

CONFIG = FingerprintConfig()


def req(*texts: str, tools: list | None = None):  # type: ignore[no-untyped-def]
    messages = [{"role": "user", "content": t} for t in texts]
    return from_openai({"messages": messages, "tools": tools or []})


def test_tokens_keep_identifiers_whole_and_drop_case_and_punctuation() -> None:
    assert lexical_tokens(req("Rename user_id to UserId, please!")) == [
        "rename",
        "user_id",
        "to",
        "userid",
        "please",
    ]


def test_tokens_ignore_what_the_fingerprint_scrubs() -> None:
    a = req("Open /home/a/proj/x.py at 2026-10-19T10:00:00Z for ticket-12")
    b = req("Open /srv/b/other/y.py at 2026-12-01T09:00:00Z for ticket-99")
    config = FingerprintConfig(extra_scrubs=(r"ticket-\d+",))
    assert lexical_tokens(a, config) == lexical_tokens(b, config)
    assert lexical_tokens(a) != lexical_tokens(b)  # without the project's rule they differ


def test_tokens_follow_tail_n() -> None:
    config = FingerprintConfig(tail_n=1)
    assert lexical_tokens(req("noise one", "the question"), config) == ["the", "question"]


def test_similarity_is_one_for_the_same_words_and_falls_with_each_difference() -> None:
    same = ["a", "b", "c", "d"]
    assert similarity(same, same) == 1.0
    assert similarity(same, ["a", "b", "c", "x"]) == pytest.approx(0.75)
    assert similarity(same, ["w", "x", "y", "z"]) == 0.0
    assert similarity([], same) == 0.0
    assert similarity(["a", "a"], ["a"]) == pytest.approx(2 / 3)  # counts matter


def test_shape_separates_roles_tools_and_required_params() -> None:
    tool = {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}
    assert shape(req("x")) == shape(req("y"))
    assert shape(req("x")) != shape(req("x", tools=[tool]))
    assert shape(req("x")) != shape(req("x", "x"))
    hot = from_openai({"messages": [{"role": "user", "content": "x"}], "temperature": 0.9})
    cold = from_openai({"messages": [{"role": "user", "content": "x"}], "temperature": 0.1})
    needs_temp = FingerprintConfig(include_params=("temperature",))
    assert shape(hot) == shape(cold)
    assert shape(hot, needs_temp) != shape(cold, needs_temp)


def sqlite_db(tmp_path: Path) -> Database:
    db = Database(fresh_database_url(tmp_path))
    if db.dialect != "sqlite":
        pytest.skip("FTS5 is the SQLite implementation")
    return db


def test_the_fts5_index_ranks_the_closest_entry_first_and_is_scoped_to_a_project(
    tmp_path: Path,
) -> None:
    db = sqlite_db(tmp_path)
    index = make_index(db)
    assert isinstance(index, Fts5Index)
    index.upsert("e1", "default", "default", ["list", "files", "in", "src"])
    index.upsert("e2", "default", "default", ["list", "files", "in", "tests", "folder", "now"])
    index.upsert("e3", "default", "other", ["list", "files", "in", "src"])
    hits = index.search("default", "default", ["list", "files", "in", "src"], CONFIG, 5)
    assert [h.entry_id for h in hits] == ["e1", "e2"]  # best first; e3 belongs to another project


def test_the_fts5_index_replaces_and_deletes(tmp_path: Path) -> None:
    db = sqlite_db(tmp_path)
    index = make_index(db)
    index.upsert("e1", "default", "default", ["alpha"])
    index.upsert("e1", "default", "default", ["beta"])  # an upsert replaces, never duplicates
    assert index.indexed_ids("default") == {"e1"}
    assert index.search("default", "default", ["alpha"], CONFIG, 5) == []
    assert [h.entry_id for h in index.search("default", "default", ["beta"], CONFIG, 5)] == ["e1"]
    index.delete("e1", "default")
    assert index.indexed_ids("default") == set()
    index.upsert("e2", "default", "p", ["gamma"])
    index.delete_project("default", "p")
    assert index.indexed_ids("default") == set()


def test_the_fts5_query_treats_odd_words_as_words_not_syntax(tmp_path: Path) -> None:
    db = sqlite_db(tmp_path)
    index = make_index(db)
    index.upsert("e1", "default", "default", ["and", "or", "not", "near", "x_1"])
    hits = index.search("default", "default", ["and", "or", "not", "near", "x_1"], CONFIG, 5)
    assert [h.entry_id for h in hits] == ["e1"]


def test_the_scan_index_is_the_fallback_when_fts_is_absent(tmp_path: Path) -> None:
    db = Database(fresh_database_url(tmp_path))
    scan = ScanIndex(db)
    assert scan.indexed_ids("default") is None  # nothing to repair
    assert scan.search("default", "default", [], CONFIG, 5) == []
