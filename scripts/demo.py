#!/usr/bin/env python3
"""Run semula on a free port with demo data, and print where the dashboard is.

    make demo                 replay mode: a log that keeps moving, plus a near-miss to review
    make demo ARGS=--record   record mode: two calls are parked, waiting for an answer
    scripts/demo.py --check   start, seed, verify the dashboard answers, stop (exit 0 if fine)

The data lives in a temporary directory that is removed on exit (``--keep`` to keep it).
Nothing here is a fixture for tests: it is what a first look should show.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
from free_port import free_port

SYSTEM = "You are a support triage bot. Reply tersely."
LOOKUP = {
    "type": "function",
    "function": {
        "name": "lookup_order",
        "description": "Find an order by its number.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
}
REVIEWER = (
    "You are a careful code reviewer. Read the function below and list every bug you can "
    "find, one per line, with the line number and a short fix. Do not rewrite the function. "
)
KNOWN = [
    "Classify this ticket as JSON with a category field: Order #A-1042 arrived with a broken "
    "screen. Please refund me.",
    "What is the refund policy for annual plans?",
    "Translate 'good morning' into French.",
    "Summarise this ticket in one sentence: the app crashes when I upload a photo.",
]
UNKNOWN = [
    "Draft a polite reply to a customer asking for an invoice copy.",
    "Is the warehouse in Penang open on public holidays?",
    "Rewrite this sentence in a friendlier tone: Your request was denied.",
    "How many tickets were opened last week?",
]


def ask(prompt: str, **extra: object) -> dict[str, object]:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    return {"model": "demo", "messages": messages, **extra}


def seed(base: str, *, record: bool) -> int:
    """Entries, calls and a near-miss. Returns how many calls it made."""
    http = httpx.Client(base_url=base, timeout=10)
    http.post("/api/v1/projects", json={"id": "triage"}).raise_for_status()

    def entry(prompt: str, response: dict, **extra: object) -> str:
        body = {"request": ask(prompt, **extra), "response": response}
        resp = http.post("/api/v1/projects/triage/entries", json=body)
        resp.raise_for_status()
        return str(resp.json()["id"])

    entry(KNOWN[0], {"content": '{"category": "refund"}'})
    entry(KNOWN[1], {"content": "Annual plans are refundable within 30 days."})
    fr = entry(KNOWN[2], {"content": "Bonjour."})
    http.put(f"/api/v1/entries/{fr}", json={"version": 1, "pinned": True}).raise_for_status()
    entry(KNOWN[3], {"content": "Photo uploads crash the app."})
    tool_call = {"message": {"content": None, "tool_calls": [{"function": {
        "name": "lookup_order", "arguments": '{"order_id": "A-1042"}'}}]}}  # fmt: skip
    entry("Where is order A-1042?", tool_call, tools=[LOOKUP])
    entry(REVIEWER + "def add(a, b): return a - b", {"content": "Line 1: it subtracts; use +."})

    calls = 0
    paths = "/v1/projects/triage/chat/completions"
    for _ in range(3):
        for prompt in KNOWN[:3]:
            http.post(paths, json=ask(prompt)).raise_for_status()
            calls += 1
    http.post(paths, json=ask("Where is order A-1042?", tools=[LOOKUP])).raise_for_status()
    # One word changed from a recorded prompt: close, but not close enough to serve.
    near = REVIEWER.replace("every bug", "every issue") + "def mul(x, y): return x + y"
    for prompt in (*UNKNOWN[:2], near):
        calls += 1
        if http.post(paths, json=ask(prompt)).status_code not in (200, 424):
            raise SystemExit("seeding hit an unexpected error")
    if record:
        http.put("/api/v1/mode", json={"mode": "record"}).raise_for_status()
        for prompt in UNKNOWN[2:]:  # parked: each call waits for an answer, so do not block
            threading.Thread(
                target=lambda p=prompt: httpx.post(base + paths, json=ask(p), timeout=600),
                daemon=True,
            ).start()
    return calls


def trickle(base: str, stop: threading.Event) -> None:
    """A call every few seconds so the live log moves."""
    rng = random.Random()  # noqa: S311 (a demo picking prompts, not security)
    paths = base + "/v1/projects/triage/chat/completions"
    while not stop.wait(rng.uniform(1.0, 3.0)):
        prompt = rng.choice(KNOWN if rng.random() < 0.7 else UNKNOWN)
        try:
            httpx.post(paths, json=ask(prompt), timeout=5)
        except httpx.HTTPError:
            return


def wait_healthy(base: str, proc: subprocess.Popen[bytes], log: Path) -> None:
    for _ in range(300):
        if proc.poll() is not None:
            sys.exit(f"semula exited early:\n{log.read_text()}")
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    sys.exit(f"semula never became healthy:\n{log.read_text()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--record", action="store_true", help="start in record mode, calls parked")
    parser.add_argument("--check", action="store_true", help="verify and exit; no live traffic")
    parser.add_argument("--keep", action="store_true", help="keep the temporary data directory")
    args = parser.parse_args()

    port = free_port(args.port)
    base = f"http://127.0.0.1:{port}"
    data = Path(tempfile.mkdtemp(prefix="semula-demo-"))
    log = data / "server.log"
    env = {**os.environ, "SEMULA_DATA_DIR": str(data), "SEMULA_MODE": "replay"}
    env.pop("SEMULA_API_KEY", None)  # the dashboard is off while a key is set
    env.pop("SEMULA_DATABASE_URL", None)  # never seed someone's real database
    with log.open("wb") as out:
        proc = subprocess.Popen(  # noqa: S603 (our own interpreter and module)
            [sys.executable, "-m", "semula", "serve", "--port", str(port)],
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # clean up on `kill` too
    try:
        wait_healthy(base, proc, log)
        calls = seed(base, record=args.record)
        url = f"{base}/ui/projects/triage/log"
        if args.check:
            page = httpx.get(url, timeout=20)
            logged = len(httpx.get(base + "/api/v1/log?limit=500").json()["calls"])
            ok = page.status_code == 200 and '<div id="app">' in page.text and logged >= calls
            print(f"{'ok' if ok else 'FAIL'}: dashboard {page.status_code}, {logged} calls logged")
            return 0 if ok else 1
        print(f"\n>>> semula demo: {url}", flush=True)
        print(f">>> API {base}/api/v1   (Ctrl+C to stop; the data is thrown away)\n", flush=True)
        if args.record:
            print(
                ">>> record mode: two calls are parked; answer them with `semula pending`\n",
                flush=True,
            )
        else:
            threading.Thread(target=trickle, args=(base, stop), daemon=True).start()
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not args.keep:
            shutil.rmtree(data, ignore_errors=True)
        else:
            print(f"data kept in {data}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
