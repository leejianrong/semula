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

One core serves about 600 hits a second, reached at 4 callers. Past about 16 callers at once throughput *falls* and the tail grows from tens of milliseconds to seconds. Nothing errors; it just gets slow. That is the first thing to give.

**Held calls** (record mode, distinct prompts, nobody answering): about 30 KB each. 2,000 held at once took the process from 144 MB to 203 MB, and `/health` still answered (273 ms at the top step, 8 ms at 500). The default hosted limit is 50 held per project and 200 per workspace, far below where this gives out, so memory is not the constraint. The compose file's 512 MB limit for the app is not close to being reached by held calls.

**One workspace flooding another** (128 callers on A while B sends hits):

| | B's hits a second | B's p95 |
|---|---:|---:|
| B alone, quotas off | 584 | 8.7 ms |
| B while A floods, quotas off | 70 | 210 ms |
| B while A floods, default quotas | (B is also limited to 20 a second) | 4.6 ms alone, 223 ms during the flood |

## What this means

- **The per-token rate limit protects the quota, not the server.** With the defaults (20 requests a second, burst 100, per token) A's flood is mostly answered `429`, and B's latency still rises about fifty-fold. A refused request still has to look the token up in the database before the limiter can see which token it is, so a refusal is not free. All callers share one worker pool, so the queue A builds is the queue B waits in.
- **A workspace can reach the limit without flooding.** 20 requests a second from each of 30 tokens is the whole core, and a workspace may hold 50 tokens. The rate limit is per token, so it does not bound a workspace.
- **Not changed**: no quota or compose default was changed on this evidence. The quotas that came out right are the held-call limits and the memory limits. The finding is about concurrency, which the defaults do not govern.

## Before opening to the public

Choose one, then rerun `isolate` on the real droplet:

1. Keep the server in the range where it behaves (cap work in flight at about 16) and shed the rest with a fast `503`, so a flood is refused before it queues.
2. Remember a token's identity for a few seconds in memory so a refused request costs microseconds. It means a revoked token works for those seconds.
3. Rate limit by workspace as well as by token.
4. Rate limit by address at the proxy, so a flood is stopped before the app sees it.

Until one is in, an invite-only server is the safe setting: the people who could flood it are the people you invited.
