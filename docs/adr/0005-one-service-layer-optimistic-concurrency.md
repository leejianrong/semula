# ADR-0005: One service layer behind every surface, with optimistic concurrency

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)

## Context

REST, CLI, MCP and the dashboard all write the same data, and one of the writers is an agent that may overwrite a human's fix.

## Decision

All surfaces call one service layer; no surface writes the database directly. Entries carry a `version`. Writes state the expected version; a stale write returns 409 with the current entry. Entries can be `pinned`; agent writes to a pinned entry fail unless forced, and forcing is logged. The CLI is a thin HTTP client with `--json` and stable exit codes. Agent-facing lists return a lean projection (no bodies).

## Amendment: entry history (SEM-66, 2026-10-21)

Every write to an entry (authored, answered from the queue, edited, pinned, imported, forced) stores a snapshot in `entry_revision` in the same transaction: version, time, actor (`agent` or `human`), a few words on what changed, whether an agent forced past a pin, and the responses, selection and pin as the write left them. Reading it is `GET /api/v1/entries/{id}/history` (`?version=N` for one revision with its snapshot), `semula entry history`, MCP `entry_history`. The latest 50 per entry are kept; deleting the entry (or its project) deletes its history; export and import carry none, and an import starts the target's history with an `imported` revision. Reads, hits and re-keys are not writes and leave none. Entries that existed before the table got one `before history` revision each. A `version_conflict` still returns the current entry; the history is what to compare it with.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Last write wins | An agent re-record could silently destroy a human fix. |
| Locks | Overkill for a local tool; versions are simpler and work across surfaces. |

## Consequences

Buys one behaviour to test and no drift between CLI and MCP. Costs clients handling 409s, which agents must be told how to do (tool descriptions).
