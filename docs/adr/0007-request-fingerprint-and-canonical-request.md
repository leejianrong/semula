# ADR-0007: Match on a canonical request with a normalised fingerprint

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)

## Context

Frameworks inject timestamps, UUIDs and absolute paths, reorder tools and add parameters like temperature that do not change the intended answer. Codex uses the Responses API and other clients use Anthropic Messages, so the matcher must not depend on one wire format.

## Decision

Convert every incoming request to an internal canonical request. Normalise: scrub timestamps, UUIDs and absolute paths (plus per-project regexes), strip JSON noise, sort tools by name. The fingerprint covers normalised messages and tools. By default `model`, `temperature`, `top_p`, `max_tokens`, `stream`, `user` and `seed` are ignored. Per-project settings can override the ignore list, set `tail_n` (match only the last N messages), and add scrub rules. Wire adapters (Chat Completions now, Responses and Anthropic later) produce canonical requests.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Hash the raw body | Misses on any noise. |
| Match on the last user message only | Conflates the same words used in different conversations. |

## Consequences

Buys robust hits and an additive path to other APIs. Costs scrubbers that can over-merge (two prompts that differ only by a scrubbed value share an entry); per-project rules and test_match help.
