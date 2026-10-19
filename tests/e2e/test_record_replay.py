"""The walking-skeleton acceptance demo (SEM-10).

A real OpenAI SDK call is parked in record mode and answered through the CLI. The
server is then restarted in replay mode and the same call is made with every
non-loopback connection refused, proving replay needs no network.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from semula.cli import main
from tests.support.db import fresh_database_url
from tests.support.server import running_server

openai = pytest.importorskip("openai")

pytestmark = pytest.mark.e2e

PROMPT = [{"role": "user", "content": "Summarise the plan in one line."}]
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture
def block_outbound(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) else None
        if host not in LOOPBACK:
            raise OSError(f"outbound connection to {address!r} blocked by the test")
        real_connect(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield


def client(url: str) -> openai.OpenAI:
    return openai.OpenAI(base_url=url + "/v1", api_key="not-used", max_retries=0, timeout=20)


def test_record_once_then_replay_offline(tmp_path: Path, block_outbound: None) -> None:
    data = tmp_path / "data"
    database_url = fresh_database_url(data)  # one database for both server runs

    with running_server(data, database_url=database_url, mode="record", record_timeout=20.0) as url:
        answered = threading.Event()

        def agent() -> None:
            for _ in range(500):
                pending = httpx.get(url + "/api/v1/pending").json()["pending"]
                if pending:
                    assert (
                        main(["--url", url, "answer", pending[0]["id"], "--text", "One line."]) == 0
                    )
                    answered.set()
                    return
                time.sleep(0.02)

        threading.Thread(target=agent, daemon=True).start()
        recorded = client(url).chat.completions.create(model="gpt-4o-mini", messages=PROMPT)
        assert answered.wait(5)
        assert recorded.choices[0].message.content == "One line."

    with running_server(data, database_url=database_url, mode="replay") as url:
        sdk = client(url)
        started = time.perf_counter()
        replayed = sdk.chat.completions.create(model="another-model", messages=PROMPT)
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert replayed.choices[0].message.content == "One line."
        assert replayed.model == "another-model"
        assert elapsed_ms < 500, f"replay took {elapsed_ms:.0f} ms"

        with pytest.raises(openai.APIStatusError) as miss:
            sdk.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "never recorded"}]
            )
        assert miss.value.status_code == 424


def test_the_outbound_guard_really_blocks(block_outbound: None) -> None:
    with pytest.raises(OSError, match="blocked by the test"):
        socket.create_connection(("192.0.2.1", 443), timeout=1)
