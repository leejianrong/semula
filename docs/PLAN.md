# semula: Plan

Status: agreed (headless core) · Milestone: MVP, local container

## Problem

Apps that call an OpenAI-compatible endpoint cost real money through OpenRouter, mostly during development and testing, when the same prompts run again and again. Hand-writing mocks for multi-turn, tool-calling flows is slow, and exact-match caches miss because frameworks add timestamps, IDs and paths to every prompt.

Meanwhile the developer already pays for generous Claude Code and Codex subscriptions that can write those responses at no extra cost.

## Solution

semula is a local container that speaks the OpenAI Chat Completions API. Point an app's base URL at it. In replay mode, a known prompt returns its recorded answer instantly with no network. In record mode, an unknown prompt is parked, a coding agent (over MCP or CLI) writes the answer, semula checks it is valid, saves it, and the app's held call completes. The next run replays it for free.

Matching tolerates noise (timestamps, UUIDs, paths, reordered tools) and never serves a weak guess silently: near-misses go to a review queue. Recordings are grouped by project and can be exported to files and committed to git. A dashboard (after the headless core) lets a human watch calls, answer pending ones, review near-misses and fix entries.

## Users and actors

1. **Coding agent** (Claude Code, Codex): sets up projects and answers pending calls through MCP or CLI. First-class.
2. **Developer**: runs the container, points apps at it, reviews and fixes entries, mostly via CLI and later the dashboard.
3. **Client app / test suite / CI**: sends LLM calls; expects a normal OpenAI response.

Agents and humans share one write path. When they conflict, the human wins: a human edit can be pinned and an agent cannot overwrite it silently (ADR-0005).

## Scope

**In this milestone**
- `/v1/chat/completions` (text, streaming, tool calls), `/v1/models`, OpenAI-shaped errors.
- Projects, entries, versions, pinning, response selection policies, replay templating.
- Record mode as a park-and-answer queue (ADR-0002); schema validation of answers.
- Matching: exact, normalised hash, BM25 above a high bar; review queue for weaker candidates (ADR-0003, ADR-0007).
- REST admin API, CLI, MCP (HTTP plus stdio shim), call log, export/import.
- One container, one port, SQLite on a volume.
- Dashboard on indah, after the headless core (ADR-0006).

**Out**
- Auth, users, multi-tenant hosting (designed, not built; ADR-0004, ADR-0006).
- Responses API and Anthropic Messages adapters (seam kept; not needed by the stated apps).
- Embeddings as a default. An optional extra only (SEM-E8).
- Spawning `claude -p` / `codex exec` per miss. Automated subscription use is unverified against provider terms (ADR-0002).
- Passing misses through to a paid provider. A later option, never the default.

## Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| R0 | A recorded LLM answer replays with no network and no API cost | Core goal |
| R1 | A coding agent can answer a parked call and the answer is saved | Must-have |
| R2 | Real OpenAI SDKs and LangChain work unchanged, including streaming, tools and errors | Must-have |
| R3 | Recordings are organised per project and can be edited without silent overwrite | Must-have |
| R4 | Noisy prompts still match, and a weak match is never served silently | Must-have |
| R5 | Agents can run all of setup and recording through MCP or CLI | Must-have |
| R6 | Every call is logged; projects export to diffable files | Must-have |
| R7 | A human dashboard for watching, answering, reviewing and fixing | Must-have, after headless |
| R8 | Ships as a small, fast single container | Must-have |
| R9 | Nothing in the MVP forecloses a hosted version | Nice-to-have |

## Success measures

- Replay hit overhead p95 < 15 ms (1k-entry project, no embeddings, excluding streaming delay), enforced in CI by `tests/perf`; the opt-in lexical tier and misses, which search the index, get 30 ms. With embeddings a miss (the only path that embeds) targets p95 < 40 ms, measured by the `embeddings` CI job.
- Image < 350 MB without the embeddings extra. Cold start < 2 s.
- Zero outbound calls in replay mode, asserted by a test with the network blocked.
- OpenAI Python SDK and LangChain pass the conformance suite (streaming, tools, errors).
- A coding agent completes a record session using only MCP tools.

## Shape

| Part | Mechanism | ADR |
|------|-----------|-----|
| S1 | One process, one port: `/v1` LLM API, `/api/v1` admin, `/mcp`, later `/ui`. | 0001 |
| S2 | Record queue: a miss writes a `pending` row; the HTTP call waits up to `record_timeout`; an answer (validated) releases it and saves an entry; a late answer is still saved. | 0002 |
| S3 | Matcher cascade: exact fingerprint, then normalised fingerprint, then BM25 (FTS5). Weak candidates enter the review queue. Embeddings are an optional tier (extra, `SEMULA_EMBEDDINGS=on`) that only feeds review unless a project opts in. | 0003, 0007 |
| S4 | Storage: SQLAlchemy Core over SQLite locally and Postgres when hosted, Alembic migrations, `workspace_id` on every table. Export/import to files carrying ULIDs. | 0004, 0009 |
| S5 | One service layer; REST, CLI, MCP and the dashboard are thin callers. Optimistic concurrency with entry versions. | 0005 |
| S6 | Streaming: store the assembled message plus chunk boundaries when recorded from a stream; replay by re-chunking or verbatim. | 0008 |
| S7 | Dashboard: indah mounted at `/ui`, designed freely, gaps fed back to indah. | 0006 |

## Affordances

**UI (after headless).**

| Affordance | Place | Wires to |
|------------|-------|----------|
| Live request log with detail pane | Log | event stream, call_log |
| Pending queue with response editor | Pending | pending, answer |
| Entries browser, project switcher, filters | Entries | entries list |
| Entry editor with conflict handling and pin | Entry | entry update |
| Review queue with request/candidate diff | Review | candidates, approve/reject |
| Playground | Playground | test_match |

**Non-UI.**

| Affordance | Kind | Wires to |
|------------|------|----------|
| `POST /v1/chat/completions` | HTTP handler | matcher, replay, record queue |
| `GET /v1/models` | HTTP handler | project settings |
| `/api/v1/*` | REST | service layer |
| `semula pending / answer / tail / export / import / test-match / serve / mcp` | CLI | REST |
| `/mcp` tools | MCP | service layer |
| Project routing by `X-Semula-Project` header or `/v1/projects/<id>/...` | middleware | project lookup |
| Call log with retention | store | event bus |

## Implementation decisions

- **Canonical request.** All matching works on an internal canonical request, not the wire format, so a Responses or Anthropic adapter is additive (ADR-0007).
- **Fingerprint** covers normalised messages and tools. `model`, `temperature`, `top_p`, `max_tokens`, `stream`, `user`, `seed` are ignored by default; per project overrides exist, as does `tail_n` for noisy history.
- **Responses.** An entry holds an ordered list with a selection policy (`first`, `sequence`, `cycle`). Replay expands `{{now}}`, `{{uuid}}`, `{{request.model}}`.
- **Failure behaviour.** Replay miss returns an OpenAI-shaped `semula_no_match` error with the nearest candidate and score. Record timeout returns `semula_record_timeout` and leaves the row pending. Bad JSON is a 400.
- **Concurrency.** Writes carry the expected entry version; a stale write is a 409 with the current entry. Pinned entries reject agent writes unless forced.
- **Security.** Local mode binds loopback by default, no auth, optional `SEMULA_API_KEY`. Authorization headers are never stored. Stored request bodies pass a configurable redaction step because recorded prompts can hold secrets.
- **Dependencies.** FastAPI, Uvicorn, FastMCP, SQLite FTS5, optional `fastembed`, indah for the dashboard. Go and PyTorch were rejected (ADR-0001).
- **Versioning.** Alembic migrations from day one; export files carry `format: semula/1`.

## indah readiness for the dashboard

Verdict from reading indah's code and board (details and the 15 requested enhancements are in `docs/INDAH-REQUESTS-PROMPT.md`):

- **Ready:** table with sort, page and select; stat tiles; charts; tabs, sidebar, grid; per-viewer sessions; light/dark; error toasts; single port, mountable as an ASGI sub-app.
- **Gaps for the local MVP (E1-E8):** server push from outside a session, richer table (row IDs, filter, server paging, cell renderers), editor and JSON/diff viewers, routing and deep links, overlays, resizable panes, dynamic children, app-level theme tokens.
- **Gaps for hosted (H1-H7):** identity in the session, login, CSRF/Origin checks, authorization, external session store, hardening in core, token UI, base-path support.

The dashboard waits on E1 and E2 at minimum. Nothing in the headless slices waits on indah.

## Testing approach

Test at the fewest, highest seams: the HTTP surface and the service layer.

- **Fast layer, no infra:** unit and integration tests run an in-process app against a temp SQLite file with a fake clock. Marked `unit` and `integration`.
- **E2E layer:** the container booted for real, driven by the OpenAI SDK and LangChain, with the network blocked in replay tests.
- Good tests assert external behaviour (what the client sees, what the database holds), not internals. Every bug gets a failing test first. Per-slice test plans are in `SLICES.md`.

## Assumed defaults

| ID | Assumed | Cost if wrong |
|----|---------|---------------|
| row 8 | Python and FastAPI; Go rejected | Medium: indah and FastMCP fit Python, a rewrite later costs weeks |
| row 9 | Single process, indah mounted in-process at `/ui` | Low: the dashboard can move to its own process later |
| D1 | Default ignored fields in the fingerprint | Low: per-project override |
| D3 | `tail_n` per project for noisy history | Low |
| D4 | `first` selection policy by default | Low |
| D5 | Instant replay; optional delay | Low |
| D8 | Chat Completions only | Medium: Codex uses the Responses API, so adapters come sooner if Codex is a client |
| row 11 | No auth locally; optional shared secret | Low locally; High if the container is exposed, hence the loopback default |

## Open risks

- **Record loop depends on agent behaviour** (polling, answering well, staying alive during a test run). Revealed by SEM slice V1 ("park a miss", then the demo). Mitigation: client timeouts documented, late answers still saved.
- **Subscription terms** for automated agent use are unchecked. Mitigation: the queue is agent-driven; spawning stays opt-in and later.
- **Dashboard timing** depends on indah delivering E1 and E2. Mitigation: headless first; the indah plan round decides dates.
- **BM25 threshold tuning** may over- or under-serve. Revealed by SEM-E6; the review queue limits the damage.
- **Large streaming loops** during test runs may flood the live log. Needs coalescing designed with indah E1/E2.
