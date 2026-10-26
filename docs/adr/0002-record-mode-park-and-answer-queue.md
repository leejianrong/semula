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

## Amendment (2026-10-26, SEM-95): claiming a call with a lease

Several workers answering one queue need to avoid each other, and listing then showing is two round trips. `claim_next` (REST `POST /api/v1/pending/claim`, `semula claim`, MCP `claim_next`) returns the oldest parked call that nobody holds, in full (the `get_pending` view plus a `claim`), and holds it for `lease_seconds` (default 300, at most 3600).

- **Storage.** Two nullable columns on `pending`, `claimed_by` and `claim_expires_at` (migration 0019; the table already has row-level security). No new table.
- **Exclusion.** One `UPDATE ... WHERE status = 'pending' AND (claim_expires_at IS NULL OR claim_expires_at <= now)` decides each claim and the caller checks the row count. A lost race looks for the next oldest. Same on SQLite and Postgres, in one process or many; no `SKIP LOCKED`, no in-process lock.
- **An expired lease is no lease.** Nothing sweeps; the expiry is compared at claim time, so a crashed worker's call is claimable again when its lease ends.
- **The lease is advisory.** It keeps other `claim_next` calls away. It does not stop `answer_pending` by anyone else, a human in the dashboard included: a person fixing a call must not be locked out by a stuck worker. An answer ends the claim because the row is no longer pending. The list view shows `claimed_by` and `claimed_until` while a lease runs.
- **Roles.** Editor (it hands out work to answer), `record` scope for tokens, and project-bound like every pending method: a token bound to one project claims only there and `claim_next` of another project is a 404. Claims are not audited (high volume, no state a person decides); the claim is visible on the list.
- **Not done.** No renew call, no priorities, no worker registry. A worker that cannot finish lets the lease run out. (The answerer hook of ADR-0014 also claims a call by id and gives it back, through the service only; there is no REST, CLI or MCP release.)
