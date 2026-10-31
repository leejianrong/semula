import pytest

from semula.config import Settings, _networks_from_env
from semula.proxy import client_address, parse_networks

pytestmark = pytest.mark.unit
NETS = parse_networks(["172.18.0.0/16", "fd00::/8"])


@pytest.mark.parametrize(
    ("peer", "forwarded", "expected"),
    [
        ("172.18.0.5", "203.0.113.9", "203.0.113.9"),
        ("172.18.0.5", "", "172.18.0.5"),  # nothing forwarded: the proxy itself
        ("198.51.100.1", "203.0.113.9", "198.51.100.1"),  # not a proxy of ours
        ("172.18.0.5", "1.2.3.4, 203.0.113.9", "203.0.113.9"),  # the client's own hop is ignored
        ("172.18.0.5", "203.0.113.9, 172.18.0.9", "203.0.113.9"),  # a second proxy
        ("172.18.0.5", "172.18.0.7, 172.18.0.9", "172.18.0.5"),  # all ours
        ("172.18.0.5", "not-an-address", "172.18.0.5"),  # unreadable: believe none of it
        ("172.18.0.5", "203.0.113.9, junk", "172.18.0.5"),
        ("fd00::5", "2001:db8::7", "2001:db8::7"),  # IPv6
        ("172.18.0.5", " 203.0.113.9 ", "203.0.113.9"),
    ],
)
def test_client_address(peer: str, forwarded: str, expected: str) -> None:
    assert client_address(peer, forwarded, NETS) == expected


def test_no_trusted_proxy_means_the_peer() -> None:
    assert client_address("172.18.0.5", "203.0.113.9", []) == "172.18.0.5"


def test_the_setting_parses_addresses_and_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _networks_from_env("") == ()
    assert _networks_from_env(" 172.18.0.0/16 , 10.0.0.1 ,") == ("172.18.0.0/16", "10.0.0.1")
    monkeypatch.setenv("SEMULA_TRUSTED_PROXIES", "172.18.0.0/16")
    assert Settings.from_env().trusted_proxies == ("172.18.0.0/16",)


@pytest.mark.parametrize("bad", ["*", "caddy", "172.18.0.0/99", "10.0.0.1, all"])
def test_a_wildcard_or_a_name_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="SEMULA_TRUSTED_PROXIES"):
        _networks_from_env(bad)
