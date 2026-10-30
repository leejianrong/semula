# Why calls park

This page explains why semula holds an unknown call open instead of failing it or calling a paid model. It does not give steps; for those, start with [Your first recording](../tutorial/first-recording.md).

## The problem

In record mode semula meets a prompt it has never seen, and something has to write the answer. The writer is a coding agent running on a subscription you already pay for. An agent works on its own schedule, and an app's HTTP client does not wait forever, so the two need a meeting point.

## What semula does

It stores the call as a *parked call* and keeps the HTTP request open. An agent or a person lists the parked calls (`semula pending`), reads one and answers it. semula checks the answer against the OpenAI response shape, and tool calls against the request's own tool schemas, saves it as an entry and releases the request. Every later identical call replays that entry.

If nobody answers within `SEMULA_RECORD_TIMEOUT` (300 s by default), the app gets a `504` with `semula_record_timeout`. The parked call stays in the list, so a late answer is still saved and the next run hits it.

## What it costs you

- An agent must be running a loop while you record. [Record with a coding agent](../tutorial/agent.md) sets that up.
- Your app's client timeout must be longer than the agent's thinking time.
- A person can always answer from the dashboard, so a stuck agent does not block you.

## Why not the alternatives

| Option | Why semula does not do it by default |
|---|---|
| Start a fresh agent process for each miss | It leans on automated use of a subscription and ties semula to each tool's flags. It exists as an opt-in, the [answerer hook](../guide/answerer.md). |
| Forward misses to a paid provider | It defeats the point, which is to cut API cost. It exists as an opt-in, [record through](../guide/record-through.md), behind a call and token cap. |
| Fail on a miss and ask you to record first | You lose the main benefit: recording while your app runs. |

## Related

- [Concepts](../concepts.md) defines parked call, pending and miss.
- [Manage a busy queue](../guide/busy-queue.md) covers leases for several workers.
