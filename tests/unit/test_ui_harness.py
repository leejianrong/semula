"""The dashboard test harness must not time out on a loaded machine.

A hosted dashboard test failed under load with ``httpcore.ReadTimeout`` on the session POST:
the first request builds the dashboard (an import of indah), which is slow on a busy box, and the
harness gave it httpx's default five seconds. The tests do not measure that setup, so it gets a
generous transport timeout; what a test asserts about latency is measured elsewhere.
"""

from typing import Any

import pytest

from tests.support import ui

pytestmark = pytest.mark.unit

ENOUGH = 30.0


class Stop(Exception):
    pass


def timeout_of(kwargs: dict[str, Any]) -> float:
    timeout = kwargs.get("timeout")
    if timeout is None:
        return 5.0  # httpx's default when none is given
    return float(getattr(timeout, "connect", timeout))


def test_the_session_post_waits_long_enough_for_a_cold_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def post(*_: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(ui.httpx, "post", post)
    with pytest.raises(Stop):
        ui.Tab("http://x", cookies={"c": "s"})
    assert timeout_of(seen) >= ENOUGH


def test_posting_an_event_waits_long_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def post(*_: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise Stop

    tab = ui.Tab.__new__(ui.Tab)  # no connection: only what send() asks of httpx
    tab.url, tab.cookies, tab.sid = "http://x", {}, "s"
    monkeypatch.setattr(ui.httpx, "post", post)
    with pytest.raises(Stop):
        tab.send({"id": "n"}, "click")
    assert timeout_of(seen) >= ENOUGH


def test_the_stream_connects_with_a_generous_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def stream(*_: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise ui.httpx.ConnectError("stop")  # _read swallows httpx errors, as it does for real

    tab = ui.Tab.__new__(ui.Tab)  # no thread: call the reader itself
    tab.url, tab.cookies, tab.sid = "http://x", {}, "s"
    monkeypatch.setattr(ui.httpx, "stream", stream)
    tab._read("/p")
    assert timeout_of(seen) >= ENOUGH


def test_the_tab_waits_for_init_long_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    waited: dict[str, float] = {}

    def wait_for(self: Any, found: Any, timeout: float = 3.0, after: int = 0) -> Any:
        waited["init"] = timeout
        raise Stop

    monkeypatch.setattr(ui.Tab, "wait_for", wait_for)
    monkeypatch.setattr(ui.Tab, "_read", lambda self, path: None)
    with pytest.raises(Stop):
        ui.Tab("http://x")
    assert waited["init"] >= ENOUGH
