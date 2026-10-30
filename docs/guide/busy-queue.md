# How to manage a busy queue

This guide shows you how to cap a runaway app, read a long agent loop as one conversation, and share one queue between several workers.

## Stop a runaway loop

A loop in the app can park hundreds of calls and cost an agent hours. `max_misses` caps the distinct misses a project takes in one run:

```bash
semula project settings game --set '{"max_misses": 50}'
```

The 51st new miss fails with 429 `semula_miss_ceiling` and is not parked; it shows as a miss in the log. A call that joins one already parked is not a new miss, and replay hits are never counted. Set `max_misses` to a new value to start a new run, or to `null` to remove the ceiling.

## Read a long game as one conversation

An agent loop resends a growing history, so each parked call looks unrelated to the last. `semula conversations` groups calls whose messages extend an earlier call's, and shows what each one added:

```bash
semula conversations --project game          # parked calls, groups of two or more
semula conversations --log                   # logged calls (those that parked, and exact hits)
```

Grouping is by prefix only: a call joins the group of the earlier call whose messages are the longest prefix of its own, with the same tools. Prompts that merely look alike stay apart. `semula show <id>` and the dashboard show the same for one call (the group, how deep it is, and the messages added since the previous call). The log keeps only a preview of each call, so a logged call is placed by the request it points at; near matches and misses that failed fast are left out. Only the newest 500 calls of a project are looked at.

## Several workers on one queue

`semula claim` takes the oldest parked call nobody holds and prints it in full (messages, tools, and the answer `shape`), holding it for a lease so another worker does not get it:

```bash
semula claim --worker alice --lease 120     # prints one call, or "nothing to claim"
semula answer <id> --text "..."             # before the lease ends
```

Two workers never get the same call. If a worker dies, its lease runs out (default 300 s, at most 3600) and the call can be claimed again. The lease is advisory: it only keeps other claims away, so a person can still answer the call in the dashboard. MCP has `claim_next` and REST has `POST /api/v1/pending/claim`; the pending list shows who holds a call and until when.

## Related

- [Concepts](../concepts.md) defines parked call, miss and pending.
- [REST API](../reference/rest.md) and [MCP tools](../reference/mcp.md) list `claim_next` and `POST /api/v1/pending/claim`.
