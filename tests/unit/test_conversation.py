"""Grouping calls whose messages extend an earlier call's (SEM-98)."""

from typing import Any

import pytest

from semula.conversation import thread

pytestmark = pytest.mark.unit


def msgs(*texts: str) -> list[dict[str, Any]]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(texts)
    ]


def test_a_call_that_extends_an_earlier_one_joins_it_and_shows_what_was_added() -> None:
    nodes = thread(
        [
            ("01", msgs("hi"), []),
            ("02", msgs("hi", "hello", "move?"), []),
            ("03", msgs("hi", "hello", "move?", "north", "again?"), []),
        ]
    )
    assert [nodes[i]["root"] for i in ("01", "02", "03")] == ["01", "01", "01"]
    assert nodes["01"]["parent"] is None and nodes["01"]["depth"] == 1
    assert (
        nodes["02"]["parent"] == "01" and nodes["02"]["added"] == msgs("hi", "hello", "move?")[1:]
    )
    assert nodes["03"]["parent"] == "02"
    assert [m["content"] for m in nodes["03"]["added"]] == ["north", "again?"]
    assert nodes["03"]["depth"] == 3


def test_unrelated_calls_stay_apart() -> None:
    nodes = thread([("01", msgs("a"), []), ("02", msgs("b"), []), ("03", msgs("b", "x", "c"), [])])
    assert nodes["01"]["root"] == "01"
    assert nodes["02"]["root"] == "02" and nodes["03"]["root"] == "02"


def test_the_longest_earlier_prefix_is_the_parent_and_branches_share_a_root() -> None:
    nodes = thread(
        [
            ("01", msgs("a"), []),
            ("02", msgs("a", "b", "c"), []),
            ("03", msgs("a", "b", "c", "d", "e"), []),
            ("04", msgs("a", "b", "c", "d", "z"), []),  # a sibling of 03
        ]
    )
    assert nodes["03"]["parent"] == "02" and nodes["04"]["parent"] == "02"
    assert {n["root"] for n in nodes.values()} == {"01"}


def test_an_identical_repeat_joins_the_first_with_nothing_added() -> None:
    nodes = thread([("01", msgs("a"), []), ("02", msgs("a"), [])])
    assert nodes["02"]["parent"] == "01" and nodes["02"]["added"] == []
    assert nodes["02"]["repeat"] is True and nodes["01"]["repeat"] is False


def test_different_tools_are_a_different_conversation() -> None:
    tool = [{"function": {"name": "move"}}]
    nodes = thread([("01", msgs("a"), []), ("02", msgs("a", "b", "c"), tool)])
    assert nodes["02"]["root"] == "02"


def test_an_earlier_longer_call_is_not_the_child_of_a_later_shorter_one() -> None:
    nodes = thread([("01", msgs("a", "b", "c"), []), ("02", msgs("a"), [])])
    assert nodes["01"]["parent"] is None and nodes["02"]["parent"] is None


def test_input_order_does_not_matter_ids_do() -> None:
    nodes = thread([("02", msgs("a", "b", "c"), []), ("01", msgs("a"), [])])
    assert nodes["02"]["parent"] == "01"
