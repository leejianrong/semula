# QUESTIONS: semula decision register

Status: `FORK` (asked the user), `ASSUMED` (default taken, reasoning recorded), `DECIDED` (user answered or prior input settled it), `DEFERRED`.
Landing column says where the answer is written up once P3 runs.

Working name: **semula** (Malay: "originally / from the start"). A record-replay fake-LLM server, OpenAI-compatible.

## Coverage checklist

| # | Category | Question | Status | Answer / default | Lands in |
|---|---|---|---|---|---|
| 1 | Primary user | Who is first-class: humans or agents? | ASSUMED | Both, with one write path. Agents (Claude Code/Codex via MCP/CLI) author and record; humans inspect, review and fix in the dashboard. When they conflict, a human edit wins and is marked `pinned` so an agent re-record cannot overwrite it silently. | PLAN §Users, ADR |
| 2 | Scope | What is in milestone 1? | ASSUMED | Local single-user container: `/v1/chat/completions` (+stream, +tool calls), `/v1/models`; projects; replay; record; CLI; MCP; indah dashboard. Out: auth, multi-user, hosted, Responses API, Anthropic Messages API, embeddings endpoint. | PLAN §Scope |
| 3 | Data model | Entities and identity | ASSUMED | `workspace` > `project` > `cassette entry` (request fingerprint + canonical request + response(s)) ; `call_log` rows for every request (hit, miss, mode, score). Stable slug IDs for projects, ULIDs for entries. Identity survives export/import because the ULID is stored in the exported file. | PLAN §Shape, ADR |
| 3a | Data model | Tenancy columns in the schema from day 1? | DECIDED F3 | see F3 | ADR |
| 4 | Storage | Where does data live and can it be diffed? | DECIDED (amended by ADR-0009) | SQLite by default, Postgres via `SEMULA_DATABASE_URL`, SQLAlchemy Core + Alembic. A SQLite file on a mounted volume is the local source of truth. `semula export/import` round-trips a project to a directory of JSON/YAML files so cassettes can be committed to git and diffed. Export lands in slice 5, not slice 1. | ADR |
| 5 | Concurrency | Two writers, one an agent | ASSUMED | Each entry has a `version`; writes carry the expected version (optimistic concurrency), a stale write is rejected with a 409 and the current entry, never silently lost. `pinned` entries reject agent writes unless `force`. | PLAN §Decisions |
| 6 | Interfaces | Surfaces and one write path | ASSUMED | One service layer; REST admin API (`/api/v1`), CLI (`semula ...`, thin HTTP client, `--json`, meaningful exit codes), MCP (streamable HTTP at `/mcp`, plus `semula mcp` stdio shim), dashboard. All four call the same service functions. Cheap read projection for agents: `list_entries` returns fingerprint, a 200-char request tail, hit count, no bodies. | ADR |
| 7 | Failure | Miss in replay mode; agent never answers in record mode; bad request | ASSUMED | Replay miss: HTTP 424-style OpenAI error body `semula_no_match` with the nearest candidate + score in the error `param`, never a made-up answer. Record miss: request is parked as `pending` and the HTTP call waits up to `record_timeout` (default 300s), then fails with `semula_record_timeout` and stays pending so a late answer is still saved. Malformed JSON: 400 OpenAI-shaped error. | PLAN §Failure |
| 8 | Dependencies | Each named | ASSUMED | FastAPI + Uvicorn (MIT/BSD), FastMCP (Apache-2.0), SQLite + FTS5 (public domain), `fastembed` + ONNX model `bge-small-en-v1.5` (Apache/MIT, offline after one-time model download, optional extra), indah (Apache-2.0, own project). Rejected: Go (embedding story needs CGO, and indah is Python), PyTorch/sentence-transformers (image size), Chroma/FAISS (SQLite brute-force over <100k vectors is enough). | ADR |
| 8a | Dependencies | How record mode gets an answer from the subscription agent | DECIDED F1 | see F1 | ADR |
| 8b | Dependencies | indah readiness and sequencing | DECIDED F2 | see F2 | PLAN §indah, SLICES |
| 9 | Runtime | Where it runs | ASSUMED | One container, one process, one port (default 8080): `/v1/*` LLM API, `/api/v1/*` admin, `/mcp`, `/ui` dashboard (indah mounted as an ASGI sub-app, ADR-0008 of indah). Volume `/data`. Host assumption: Docker or plain `uvx semula serve`. | ADR |
| 10 | Success | Measurable | ASSUMED | Replay hit overhead p95 < 15 ms excluding streaming delay (1k-entry project, no embeddings); with embeddings p95 < 40 ms; container image < 350 MB without embeddings extra; cold start < 2 s; 0 paid-API calls in replay mode (asserted by a test with outbound network blocked); an OpenAI Python SDK and a LangChain client both pass a conformance suite against streaming, tools and error shapes. | PLAN §Success |
| 11 | Security | Local trust model; secrets | ASSUMED | M1 binds `127.0.0.1` by default in `uvx`, `0.0.0.0` inside the container but documented to publish only on loopback. No auth locally; optional `SEMULA_API_KEY` shared secret checked on `/v1`, `/api`, `/mcp` if set. Never log `Authorization`; request bodies are stored (they are the product) so docs warn that recorded prompts may contain secrets and a configurable redaction list scrubs headers and listed regexes before storage. Hosted auth: see ADR (design only). | PLAN §Security, ADR |
| 12 | Versioning | Schema and file format | ASSUMED | Numbered SQL migrations from day 1 (`schema_version` table); export files carry `format: semula/1`. | ADR |

## Domain categories added

| # | Category | Question | Status | Answer / default | Lands in |
|---|---|---|---|---|---|
| D1 | Matching | Which request fields are in the fingerprint | ASSUMED | Normalised messages (roles + content, dynamic values scrubbed: timestamps, UUIDs, absolute paths, configurable regex), tools sorted by name. Ignored by default: `model`, `temperature`, `top_p`, `max_tokens`, `stream`, `user`, `seed`. Per-project override. | ADR |
| D2 | Matching | Tier order and what auto-serves | DECIDED F4 | see F4 | ADR |
| D3 | Matching | Multi-turn context | ASSUMED | The fingerprint covers the whole message list, so conversations match turn by turn automatically. A `tail_n` option (match last N messages only) per project for agents whose history grows with noise. | ADR |
| D4 | Responses | Several responses for one fingerprint | ASSUMED | Ordered list with a `selection` policy: `first` (default), `sequence` (nth call returns nth), `cycle`. | PLAN |
| D5 | Streaming | How a response is stored and replayed | ASSUMED | Store the final assembled message (text, tool_calls, finish_reason, usage) plus, if recorded from a real stream, the chunk boundaries. Replay streams stored chunks, or re-chunks the assembled message (default 24 chars, configurable) when none. Default instant; `replay_delay_ms` optional. | ADR |
| D6 | Recording | Dynamic values in recorded responses | ASSUMED | Entries support `{{now}}`, `{{uuid}}`, `{{request.model}}` templating at replay. Agent is instructed (via MCP tool description) to template dynamic fields. | PLAN |
| D7 | Recording | Is the agent answering a request authoritative about format | ASSUMED | Submitted responses are validated server-side against the OpenAI response schema (and tool_calls against the request's tool JSON schemas) before saving; invalid submissions are rejected with a message the agent can act on. | PLAN |
| D8 | API surface | Which provider APIs | ASSUMED | Chat Completions only in M1 (it is the user's stated need). Internal canonical request type isolates the matcher from the wire format, so Responses API (Codex) and Anthropic Messages are additive adapters later. | ADR |
| D9 | Legal | Subscription terms for automated agent use | DECIDED F1 | see F1 (risk noted: not verified) | ADR |
| D10 | Observability | What gets logged | ASSUMED | Every call writes a `call_log` row (time, project, mode, outcome tier, score, latency, entry id). Dashboard live view tails it via an in-process event bus. Retention: 30 days or 50k rows, configurable. | PLAN |

## Decisions from the user (2026-10-19)

All four recommendations accepted. F2 was extended; the extension is binding.

| ID | Decision |
|---|---|
| F1 | Record mode is a park-and-answer queue. A miss parks the HTTP request as `pending`; the agent fetches pending requests and submits responses over MCP or CLI; submissions are schema-validated before saving. Spawning `claude -p` / `codex exec` is an optional later adapter, opt-in, because automated subscription use is unverified against provider terms. |
| F2 | Headless core first (API, replay, record, CLI, MCP), dashboard after. **Extension:** the dashboard look and feel is designed freely, not bound by what indah can do today. The design is saved in-repo (`docs/design/`). Every delta indah cannot do today becomes a request to the indah team (cards on Pandan board IND, board 30), so indah improves as a result. MVP ships on indah plus those enhancements. Before the hosted version, re-decide: stay on indah, or move to Svelte + Vite + TypeScript. |
| F3 | Hosted = shared cassette store + replay + remote MCP for recording from anywhere. `workspace_id` on every table now; no auth built in the MVP; hosted auth designed as an ADR only. |
| F4 | Exact and normalised-hash matches auto-serve. BM25 auto-serves only above a high bar. Embedding matches never auto-serve by default; they go to a review queue, with an opt-in per-project threshold. Embeddings (`fastembed`) are an optional later slice. |

## Standing instruction for indah work (this session only)

For planning today, indah's existing ADRs that rule things out (e.g. ADR-0008 "no built-in auth") are ignored: treat indah as able to grow anything the design needs. Whether to keep indah for the hosted version is decided later (see F2).

## Decided since the register was written

| Date | Decision | Where |
|---|---|---|
| 2026-10-19 | The original plan stands: Python/FastAPI, record queue, safe tiered matching, SQLite with `workspace_id` | ADRs 0001-0008 |
| 2026-10-19 | PRs are merged by the agent as soon as CI is green; `main` is protected (PR-only, all jobs required) | AGENTS.md |
| 2026-10-19 | SQLAlchemy Core + Alembic now, SQLite local and Postgres hosted; BM25 must sit behind a per-backend interface | ADR-0009 |
| 2026-10-19 | The free-form dashboard design waits until the headless core is further along; the indah requests prompt was handed to the indah agent, its plan is awaited | SEM-E9, `docs/INDAH-REQUESTS-PROMPT.md` |
| 2026-10-19 | MCP authorization (OAuth 2.1 resource server: RFC 9728 metadata, token audience validation, scope challenges) is a hosted-only item. RFC 9207 `iss` checks and CIMD are client and authorization-server duties. Today: the shared `SEMULA_API_KEY` only | SEM-53 |
| 2026-10-19 | Codex MCP registration syntax is unverified; the CLI is the fallback | `docs/agents/PLAYBOOK.md` |
| 2026-10-20 | Lexical tier: FTS5 retrieves, word-overlap scores; auto-serve default 1.0 (near-misses go to review; lower it per project once tuned) plus a same-shape guard; review floor 0.6 | ADR-0003 amendment |
| 2026-10-20 | Redaction patterns double as scrubs when fingerprinting, so the live request, its redacted stored copy and a later re-key agree; built-in credential patterns are on by default; recorded answers are not redacted | ADR-0007 amendment |
| 2026-10-20 | Replay budgets: exact and normalised p95 < 15 ms; the lexical tier and misses < 30 ms (they search the index). Image < 350 MB, cold start < 2 s (container start to healthy, MCP built after startup) | `tests/perf`, `scripts/check_image.sh` |
| 2026-10-20 | Embeddings: opt-in extra plus `SEMULA_EMBEDDINGS=on`; model cached on the volume; indexed in the background, queried only after every cheaper tier missed; paraphrases go to review, serving is a per-project opt-in (`embedding_serve_threshold`, off) | ADR-0003 amendment |
| 2026-10-20 | Dashboard design proposed in `docs/design/` (eight artboards: six local screens, two hosted sketches): the tier rail as the signature, a mode lamp, red only for "waiting on you". Six semula API gaps found by drawing real data; none built | `docs/design/README.md` |
| 2026-10-20 | Dashboard design, second round: Material 3 tokens (generated from seed #0E7C66, light and dark, the logo red as a custom `record` role), a text budget (four checks; explanations behind keyboard-accessible ⓘ tooltips), logo option D, "again from the start" | `docs/design/` |
| 2026-10-20 | MCP exposes approve and reject to agents, with a description telling them to wait for a human unless asked | `docs/agents/PLAYBOOK.md` |
| 2026-10-21 | The five headless-core gaps from the dashboard design are built (SEM-E13): redacted 120-character preview per call-log row; `GET /api/v1/log/stats`; per-tier verdicts in `test_match`; `entry_revision` history (latest 50 per entry); entries list filters, sort and flags | `docs/design/README.md` |
| 2026-10-21 | Log stats: `median_latency_ms` covers hits and misses only (a recorded or timed-out call waited for an agent); `hit_rate` is hits over every call; `parked` ignores the window | SEM-65 |
| 2026-10-21 | `has_tools` on an entry means an answer calls a tool; the list filter is a LIKE on the stored JSON, checked against the per-row flag | SEM-64 |

## Deferred

| Item | Why |
|---|---|
| Responses API / Anthropic Messages adapters | Not needed for stated apps; seam kept (D8). |
| Hosted auth implementation (OIDC login, per-project API tokens, quotas, billing) | Designed in ADR, built after M1 is used for real. |
| Cassette sharing between workspaces / public cassette library | Needs hosted. |
| Intent classifier / router model | BM25 + normalisation covers it until measured otherwise. |
