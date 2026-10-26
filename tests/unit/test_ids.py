"""Ids sort in the order they were made, even within one millisecond."""

import threading

import pytest

from semula.ids import new_ulid

pytestmark = pytest.mark.unit


def test_ids_made_in_one_millisecond_still_sort_in_creation_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("semula.ids.time.time", lambda: 4_000_000.123)  # one frozen millisecond
    ids = [new_ulid() for _ in range(500)]
    assert ids == sorted(ids) and len(set(ids)) == 500


def test_ids_in_a_tight_loop_are_strictly_increasing() -> None:
    ids = [new_ulid() for _ in range(5000)]
    assert all(a < b for a, b in zip(ids, ids[1:], strict=False))


def test_an_explicit_time_is_used_as_given() -> None:
    assert new_ulid(1_000) < new_ulid(2_000)
    assert new_ulid(2_000)[:10] == new_ulid(2_000)[:10]  # the time part is exactly the one asked
    assert new_ulid(1_000)[:10] != new_ulid(2_000)[:10]


def test_ids_from_many_threads_are_unique_and_each_threads_are_ordered() -> None:
    made: list[list[str]] = [[] for _ in range(8)]

    def work(slot: int) -> None:
        made[slot] = [new_ulid() for _ in range(500)]

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({i for chunk in made for i in chunk}) == 8 * 500
    assert all(chunk == sorted(chunk) for chunk in made)


def test_a_clock_that_goes_backwards_does_not_make_ids_go_backwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("semula.ids.time.time", lambda: 9_000_000.0)
    later = new_ulid()
    monkeypatch.setattr("semula.ids.time.time", lambda: 8_000_000.0)
    assert new_ulid() > later
