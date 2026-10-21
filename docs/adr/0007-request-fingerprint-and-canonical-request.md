# ADR-0007: Match on a canonical request with a normalised fingerprint

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)

## Context

Frameworks inject timestamps, UUIDs and absolute paths, reorder tools and add parameters like temperature that do not change the intended answer. Codex uses the Responses API and other clients use Anthropic Messages, so the matcher must not depend on one wire format.

## Decision

Convert every incoming request to an internal canonical request. Normalise: scrub timestamps, UUIDs and absolute paths (plus per-project regexes), strip JSON noise, sort tools by name. The fingerprint covers normalised messages and tools. By default `model`, `temperature`, `top_p`, `max_tokens`, `stream`, `user` and `seed` are ignored. Per-project settings can override the ignore list, set `tail_n` (match only the last N messages), and add scrub rules. Wire adapters (Chat Completions now, Responses and Anthropic later) produce canonical requests.

## Amendment: redaction (SEM-E7, 2026-10-20)

Recorded prompts can hold secrets, so stored requests are redacted: built-in credential patterns (on by default, `redact_defaults`) plus a per-project `redact_patterns` list. A match becomes `[REDACTED]` in everything stored (entries, pending requests, review candidates, aliases, imports), and changing the list re-redacts what is already stored. The same patterns are applied when fingerprinting and tokenising, so the live request, its stored copy and a later re-key all land on the same normalised fingerprint and a prompt whose secret changes still matches. The exact fingerprint is taken from the live request, so it only matches a repeat of the same secret; the normalised tier covers the rest. Authorization and other headers are never stored. Recorded answers are not redacted.

Call-log previews (SEM-63): each log row keeps a 120-character preview of the last message with text. It is redacted with the same patterns on the whole message *before* it is cut, so a secret is never half shown, and it is re-redacted when the list changes. A secret already sliced by the cut before a new pattern existed cannot be recognised again.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Hash the raw body | Misses on any noise. |
| Match on the last user message only | Conflates the same words used in different conversations. |

## Consequences

Buys robust hits and an additive path to other APIs. Costs scrubbers that can over-merge (two prompts that differ only by a scrubbed value share an entry); per-project rules and test_match help.
