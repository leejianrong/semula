# ADR-0005: One service layer behind every surface, with optimistic concurrency

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)

## Context

REST, CLI, MCP and the dashboard all write the same data, and one of the writers is an agent that may overwrite a human's fix.

## Decision

All surfaces call one service layer; no surface writes the database directly. Entries carry a `version`. Writes state the expected version; a stale write returns 409 with the current entry. Entries can be `pinned`; agent writes to a pinned entry fail unless forced, and forcing is logged. The CLI is a thin HTTP client with `--json` and stable exit codes. Agent-facing lists return a lean projection (no bodies).

## Alternatives considered

| Option | Why not |
|--------|---------|
| Last write wins | An agent re-record could silently destroy a human fix. |
| Locks | Overkill for a local tool; versions are simpler and work across surfaces. |

## Consequences

Buys one behaviour to test and no drift between CLI and MCP. Costs clients handling 409s, which agents must be told how to do (tool descriptions).
