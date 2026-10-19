# ADR-0002: Record mode is a park-and-answer queue

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)

## Context

On a cache miss in record mode, something must write the answer. The answerer is a coding agent on a Claude Code or Codex subscription. Subscriptions are not API keys, and automated headless use of them has not been checked against provider terms. Client SDKs have timeouts, and agents run on their own schedule.

## Decision

On a miss in record mode, semula stores a `pending` row and holds the HTTP call for up to `record_timeout` (default 300 s). An agent lists pending calls and submits a response through MCP or CLI. semula validates the response against the OpenAI schema, and tool calls against the request's tool schemas, saves an entry, and releases the call. On timeout the call fails with `semula_record_timeout` and the row stays pending, so a late answer is still saved. Spawning `claude -p` or `codex exec` per miss is an optional later adapter, off by default.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Spawn the agent CLI per miss | Depends on automated subscription use that is unverified against terms, and ties semula to each CLI's flags. |
| Forward misses to a paid provider | Defeats the cost goal; may be offered later as an explicit option. |
| Fail on miss and ask the user to pre-record | Loses the main value: recording while the app runs. |

## Consequences

Works with any agent and needs no provider credentials. Costs: an agent must be running a loop while recording, and client timeouts must be set above the agent's response time (documented in the playbook). Pending rows need a way to be answered by a human too (dashboard, SEM-E10).
