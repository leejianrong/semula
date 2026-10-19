# ADR-0008: Store the assembled message plus chunk boundaries; replay by re-chunking or verbatim

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)

## Context

Most apps stream. An agent answers with a whole message, while a stream recorded from a real provider has natural chunk boundaries. Replay must be accepted by SDK stream iterators.

## Decision

An entry stores the final message (content, tool_calls, finish_reason, usage). If recorded from a stream it also stores the chunk boundaries. Replay in stream mode emits stored chunks verbatim, or re-chunks the message (default 24 characters, configurable) when none exist, ending with `data: [DONE]`. Default is instant; an optional per-project `replay_delay_ms` simulates pacing. A non-stream request can be served from any entry and the reverse.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Store only chunks | Bad for agent-written answers and non-stream replay. |
| Always replay at recorded timing | Slows tests with no default benefit. |

## Consequences

Buys one entry serving both modes. Costs re-chunked output that differs from a real provider's boundaries, which only matters to apps that inspect them.
