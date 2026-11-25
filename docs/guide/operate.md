# Operate a public server

What one small server carries, measured, so you know the limits before someone finds them. The numbers below come from `scripts/loadtest.py`, which you can point at your own server.

!!! warning "These are laptop numbers, not droplet numbers"
    The first run used one core of an i7-10700 (pinned with `taskset`), hosted mode, SQLite, no Caddy, and a load generator on other cores. A 1 vCPU droplet is slower per core and adds TLS, so treat every figure as an upper bound and rerun the script against your server before you rely on it (`scripts/loadtest.py replay|parked|isolate`).

## What it measured

**Replay hits** (one recorded answer, `POST /v1/chat/completions`, quotas off so the machine is the limit):

| Callers at once | Hits a second | p50 | p95 |
|---:|---:|---:|---:|
| 1 | 388 | 2.5 ms | 2.7 ms |
| 4 | 600 | 6.6 ms | 8.5 ms |
| 16 | 609 | 24 ms | 39 ms |
| 64 | 296 | 136 ms | 712 ms |
| 256 | 186 | 707 ms | 5.2 s |

One core serves about 600 hits a second, reached at 4 callers; three load generators at once also totalled about 645, so the server's core is the limit, not the generator. From a single generator, throughput fell and the tail grew past 16 callers, which may be partly the generator's own limit. Nothing errors; it just gets slower as callers pile up.

**Held calls** (record mode, distinct prompts, nobody answering): about 30 KB each. 2,000 held at once took the process from 144 MB to 203 MB, and `/health` still answered (273 ms at the top step, 8 ms at 500). The default hosted limit is 50 held per project and 200 per workspace, far below where this gives out, so memory is not the constraint. The compose file's 512 MB limit for the app is not close to being reached by held calls.

**One workspace flooding another** (128 callers on A, in their own process, while B sends hits from another; B's p95 is what to read):

| | B's hits a second | B's p95 |
|---|---:|---:|
| Default quotas, B alone | 32 (B is held to 20 a second) | 4.2 ms |
| Default quotas, A flooding | 22 | 4.1 ms |
| Quotas off, B alone | 626 | 8.4 ms |
| Quotas off, A flooding | 359 | 17 ms |

## What this means

- **The default limits isolate tenants.** A flood from one token is answered `429` at the rate limit, and another workspace's replay latency does not move. This is the done-when for SEM-121.
- **With the limits off, tenants share the core.** A flood that is actually served takes its share of one core, and B gets about half. That is arithmetic, not a defect, and it is why the limits stay on in a hosted server.
- **A workspace can still reach the limit without flooding.** The rate limit is per token. 30 tokens at 20 requests a second is about the whole core, and a workspace may hold 50 tokens. A rate limit per workspace would close this; it is not built.
- **Measure the flood from another process.** A first version of the `isolate` run flooded and timed B in one process and reported B's latency rising fifty-fold. That was the load generator timing its own saturated event loop. The script now runs the flood as a separate process.
- **Not changed**: no quota or compose default was changed on this evidence.

## Before opening to the public

1. Run `scripts/loadtest.py` against the real droplet and replace the laptop numbers above. Do it from another machine.
2. Decide on a per-workspace rate limit (see above) and on how many tokens a workspace may hold.
3. Set the CPU and disk alerts on the droplet, and the budget alert on the backup bucket, so a flood or a full disk reaches a person.
