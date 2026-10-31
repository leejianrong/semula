import pytest

from semula.ratelimit import AddressBuckets, address_key

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_burst_passes_then_the_next_waits() -> None:
    clock = Clock()
    buckets = AddressBuckets(per_minute=30, clock=clock)
    assert [buckets.take("203.0.113.1") for _ in range(30)] == [0.0] * 30
    wait = buckets.take("203.0.113.1")
    assert 1.0 < wait <= 2.0  # 30 a minute refills one every two seconds


def test_it_refills_with_time_and_keeps_addresses_apart() -> None:
    clock = Clock()
    buckets = AddressBuckets(per_minute=30, clock=clock)
    for _ in range(30):
        buckets.take("203.0.113.1")
    assert buckets.take("203.0.113.1") > 0
    assert buckets.take("203.0.113.2") == 0.0
    clock.now += 2.0
    assert buckets.take("203.0.113.1") == 0.0
    assert buckets.take("203.0.113.1") > 0


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("203.0.113.1", "203.0.113.1", True),
        ("203.0.113.1", "203.0.113.2", False),
        ("2001:db8:1:2::1", "2001:db8:1:2:ffff::9", True),  # one /64 is one customer
        ("2001:db8:1:2::1", "2001:db8:1:3::1", False),
        ("::ffff:203.0.113.1", "203.0.113.1", True),  # an IPv4 address in an IPv6 socket
        ("unknown", "unknown", True),
    ],
)
def test_an_ipv6_customer_is_a_whole_64(a: str, b: str, same: bool) -> None:
    assert (address_key(a) == address_key(b)) is same


def test_memory_is_bounded_by_pruning_idle_addresses() -> None:
    clock = Clock()
    buckets = AddressBuckets(per_minute=30, clock=clock, max_addresses=100, prune_every=10)
    for i in range(500):
        buckets.take(f"198.51.{i // 250}.{i % 250}")
        clock.now += 0.1
    assert len(buckets) <= 120  # idle ones were dropped as they refilled; a few stay
