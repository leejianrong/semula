#!/usr/bin/env python3
"""Drive a semula server until something gives (SEM-121). Works against any URL.

    loadtest.py replay   URL TOKEN                 hits at rising concurrency: req/s and latency
    loadtest.py parked   URL TOKEN [--pid PID]     misses held in record mode: how many it carries
    loadtest.py isolate  URL TOKEN_A TOKEN_B       does a flood from A slow B's replay hits

The server needs the hosted quotas off for a capacity run (SEMULA_QUOTA_RATE=none and so on),
or the first limit you meet is the quota, not the machine. Each token needs the admin scope.
"""

from __future__ import annotations

import argparse
import asyncio
import time

import httpx

PROMPT = {"model": "semula", "messages": [{"role": "user", "content": "load test prompt"}]}


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def seed(c: httpx.AsyncClient, token: str, project: str = "default") -> None:
    r = await c.post(
        f"/api/v1/projects/{project}/entries", headers=hdr(token),
        json={"request": PROMPT, "response": {"content": "ok"}},
    )  # fmt: skip
    assert r.status_code in (201, 409), r.text


async def hammer(url: str, token: str, workers: int, seconds: float, lats: list[float],
                 codes: dict[int, int]) -> None:  # fmt: skip
    limits = httpx.Limits(max_connections=workers, max_keepalive_connections=workers)
    async with httpx.AsyncClient(base_url=url, limits=limits, timeout=60, verify=False) as c:
        end = time.monotonic() + seconds

        async def one() -> None:
            while time.monotonic() < end:
                t = time.perf_counter()
                try:
                    r = await c.post("/v1/chat/completions", headers=hdr(token), json=PROMPT)
                    code = r.status_code
                except httpx.HTTPError:
                    code = 0
                lats.append((time.perf_counter() - t) * 1000)
                codes[code] = codes.get(code, 0) + 1

        await asyncio.gather(*(one() for _ in range(workers)))


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else float("nan")


def line(label: str, lats: list[float], codes: dict[int, int], seconds: float) -> str:
    ok = codes.get(200, 0)
    bad = {k: v for k, v in codes.items() if k != 200}
    rate = f"{label:>14}  {ok / seconds:7.0f} ok/s"
    spread = f"p50 {pct(lats, 0.5):6.1f}  p95 {pct(lats, 0.95):7.1f}  p99 {pct(lats, 0.99):7.1f} ms"
    return f"{rate}  {spread}  other {bad or '-'}"


async def replay(a: argparse.Namespace) -> None:
    async with httpx.AsyncClient(base_url=a.url, verify=False) as c:
        await seed(c, a.token)
    for n in a.steps:
        lats: list[float] = []
        codes: dict[int, int] = {}
        await hammer(a.url, a.token, n, a.seconds, lats, codes)
        print(line(f"{n} workers", lats, codes, a.seconds), flush=True)


def rss_mb(pid: int | None) -> str:
    if not pid:
        return "-"
    for row in open(f"/proc/{pid}/status"):
        if row.startswith("VmRSS"):
            return f"{int(row.split()[1]) / 1024:.0f} MB"
    return "-"


async def parked(a: argparse.Namespace) -> None:
    """Park calls with distinct prompts in a project that is in record mode, and watch the
    server: memory, and how long /health takes with that many connections held."""
    async with httpx.AsyncClient(base_url=a.url, verify=False, timeout=30) as c:
        await c.post("/api/v1/projects", headers=hdr(a.token), json={"id": "park"})
        r = await c.put(
            "/api/v1/mode", headers=hdr(a.token), json={"mode": "record", "project": "park"}
        )
        assert r.status_code == 200, r.text
    held: list[asyncio.Task[int]] = []
    client = httpx.AsyncClient(base_url=a.url, verify=False, timeout=a.hold + 30,
                               limits=httpx.Limits(max_connections=None))  # fmt: skip

    async def park(i: int) -> int:
        body = {"model": "semula", "messages": [{"role": "user", "content": f"parked {i}"}]}
        try:
            r = await client.post(
                "/v1/projects/park/chat/completions", headers=hdr(a.token), json=body
            )
            return r.status_code
        except httpx.HTTPError:
            return 0

    for target in a.steps:
        while len(held) < target:
            held.append(asyncio.create_task(park(len(held))))
        await asyncio.sleep(a.settle)
        t = time.perf_counter()
        try:
            h = (await client.get("/health")).status_code
        except httpx.HTTPError:
            h = 0
        ms = (time.perf_counter() - t) * 1000
        done = [t for t in held if t.done()]
        refused = sum(1 for t in done if t.result() not in (0, 200))
        print(f"{target:>6} held  rss {rss_mb(a.pid):>8}  /health {h} in {ms:6.0f} ms  "
              f"ended early {len(done)} (refused {refused})", flush=True)  # fmt: skip
    for t in held:
        t.cancel()
    await client.aclose()


async def isolate(a: argparse.Namespace) -> None:
    async with httpx.AsyncClient(base_url=a.url, verify=False) as c:
        await seed(c, a.token)
        await seed(c, a.token_b)

    async def b_probe(label: str) -> None:
        lats: list[float] = []
        codes: dict[int, int] = {}
        await hammer(a.url, a.token_b, 4, a.seconds, lats, codes)
        print(line(label, lats, codes, a.seconds), flush=True)

    await b_probe("B alone")
    flood_l: list[float] = []
    flood_c: dict[int, int] = {}
    flood = asyncio.create_task(hammer(a.url, a.token, a.flood, a.seconds + 2, flood_l, flood_c))
    await asyncio.sleep(1)
    await b_probe("B during flood")
    await flood
    print(line("A (the flood)", flood_l, flood_c, a.seconds + 2))


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay")
    r.add_argument("url")
    r.add_argument("token")
    r.add_argument("--steps", type=int, nargs="+", default=[1, 4, 16, 64, 128, 256])
    r.add_argument("--seconds", type=float, default=10)
    r.set_defaults(func=replay)
    k = sub.add_parser("parked")
    k.add_argument("url")
    k.add_argument("token")
    k.add_argument("--steps", type=int, nargs="+", default=[50, 200, 500, 1000, 2000])
    k.add_argument("--pid", type=int)
    k.add_argument("--settle", type=float, default=3)
    k.add_argument("--hold", type=float, default=300)
    k.set_defaults(func=parked)
    i = sub.add_parser("isolate")
    i.add_argument("url")
    i.add_argument("token")
    i.add_argument("token_b")
    i.add_argument("--flood", type=int, default=128)
    i.add_argument("--seconds", type=float, default=10)
    i.set_defaults(func=isolate)
    a = p.parse_args()
    asyncio.run(a.func(a))


if __name__ == "__main__":
    main()
