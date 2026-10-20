# semula: Slices

Vertical increments, headless first. **The live source of status is Pandan board `SEM` (board 39).** This file is a snapshot of it, regenerated 2026-10-20: 50 of 61 stories done. Slice SEM-E2 confronted the riskiest mechanism: parking a call and answering it from a coding agent.

Decisions behind these: see [QUESTIONS.md](QUESTIONS.md) and [docs/adr](adr/). Cards marked ✔ are done and merged to `main`.

## SEM-E1 · Plan written down (2/2)

**Goal** — The plan, ADRs and slices exist in the repo so any agent can build from them.

**Done when** — PLAN.md, SLICES.md, QUESTIONS.md and the load-bearing ADRs are committed and consistent.

**Not in scope** — Implementation.

### ✔ SEM-1 · Write PLAN.md from the decision register (3 pts)

**Why** — The register (QUESTIONS.md) holds decisions but no narrative an agent can build from.

**Do** — PLAN.md with problem, users, scope, requirements, shape, indah readiness, success numbers, out of scope.

**Acceptance**
- [x] PLAN.md cites ADRs by number
- [x] Every requirement maps to a slice in SLICES.md

### ✔ SEM-2 · Write the load-bearing ADRs (3 pts)

**Why** — Future agents would re-litigate stack, matching, record queue and storage choices.

**Do** — One ADR each: Python/FastAPI, record queue (F1), tiered matching (F4), SQLite + workspace_id (F3), single-process runtime.

**Acceptance**
- [x] Each ADR lists rejected alternatives
- [x] ADR numbers are referenced from PLAN.md

## SEM-E2 · Record once, replay forever (walking skeleton) (8/8)

**Goal** — An OpenAI SDK call is parked, answered by a coding agent, saved, and replayed instantly with no network.

**Done when** — With outbound network blocked, the second identical call returns the recorded answer in under 15 ms.

**Not in scope** — Streaming, tools, fuzzy matching, dashboard.

### ✔ SEM-3 · Scaffold the service and container (2 pts)

**Why** — Nothing can be built or run until a service boots in a container.

**Do** — FastAPI app, config via env, /health, Dockerfile, uv project, make targets for check.

**Acceptance**
- [x] docker run serves /health on :8080
- [x] make check runs lint and tests

### ✔ SEM-4 · Create the SQLite store with migrations (3 pts)

**Why** — Projects, entries and the call log need a stable schema with a path to change it.

**Do** — Migration runner plus tables: workspace, project, entry, call_log, pending. workspace_id on every table.

**Acceptance**
- [x] schema_version table exists
- [x] Migrations apply to an empty file and are idempotent

**Not in scope** — Auth or users.

### ✔ SEM-5 · Normalise a request and compute its fingerprint (3 pts)

**Why** — Frameworks add timestamps, UUIDs and paths, so raw hashing misses.

**Do** — Canonical request type, scrubbers for time, UUID and absolute paths, tools sorted by name, stable hash. Ignore model, temperature, stream by default.

**Acceptance**
- [x] Two requests differing only in a timestamp share a fingerprint
- [x] Reordered tools share a fingerprint

**Not in scope** — Fuzzy matching.

### ✔ SEM-6 · Replay a recorded answer on /v1/chat/completions (3 pts)

**Why** — The core value: a client points at semula and gets an answer for free.

**Do** — Non-streaming endpoint; exact fingerprint hit returns the stored OpenAI-shaped response and bumps hit_count.

**Acceptance**
- [x] OpenAI Python SDK gets the stored answer
- [x] A miss in replay mode returns a semula_no_match error with HTTP 4xx

**Not in scope** — Streaming.

### ✔ SEM-7 · Park a miss as pending in record mode (5 pts)

**Why** — Record mode must hold the client call until an agent answers; this is the riskiest mechanism.

**Do** — On miss in record mode, store a pending row and wait up to record_timeout for an answer; late answers are still saved.

**Acceptance**
- [x] A held call returns once an answer is submitted
- [x] A timed-out call returns semula_record_timeout and the pending row remains

**Not in scope** — Spawning an agent process.

### ✔ SEM-8 · List and answer pending requests from the CLI (3 pts)

**Why** — The agent needs a way to see parked requests and submit responses.

**Do** — semula pending and semula answer <id> --file/--stdin, with --json and exit codes.

**Acceptance**
- [x] pending lists the parked request
- [x] answer releases the held HTTP call

### ✔ SEM-9 · Validate submitted responses against the OpenAI schema (3 pts)

**Why** — A malformed answer would be saved and break every replay.

**Do** — Reject invalid chat completion bodies with a message the agent can act on.

**Acceptance**
- [x] Invalid body is rejected, not saved
- [x] Valid body is saved and replayed

### ✔ SEM-10 · Demo: record then replay with network blocked (2 pts)

**Why** — Proves the claim of zero API cost in replay.

**Do** — Script that records one call with an agent, then replays it with outbound network denied.

**Acceptance**
- [x] Second call succeeds offline
- [x] Recorded in docs as the acceptance demo

## SEM-E3 · Streaming and tool calls that real SDKs accept (5/5)

**Goal** — Streaming and tool-calling clients work unchanged against semula.

**Done when** — OpenAI SDK and LangChain pass a conformance suite for streaming, tools and errors.

**Not in scope** — Responses API, Anthropic Messages.

### ✔ SEM-11 · Replay streaming responses as SSE (3 pts)

**Why** — Most apps set stream: true.

**Do** — Re-chunk the stored message (default 24 chars) into SSE ending with [DONE]; usage and finish_reason correct.

**Acceptance**
- [x] SDK stream iterator yields the full text
- [x] Chunk size is configurable

### ✔ SEM-12 · Capture real stream chunk boundaries when recorded (3 pts)

**Why** — Faithful replay of a recorded stream needs its chunk boundaries.

**Do** — Store chunk boundaries when an answer arrives as a stream; replay them verbatim.

**Acceptance**
- [x] Replayed chunks equal recorded chunks
- [x] Falls back to re-chunking when none stored

### ✔ SEM-13 · Validate tool_calls against the request's tool schemas (3 pts)

**Why** — Agent-written tool calls can violate the app's schemas.

**Do** — Check function names and argument JSON against the request tools before saving.

**Acceptance**
- [x] Unknown tool name is rejected
- [x] Arguments failing the schema are rejected with the path

### ✔ SEM-14 · Serve /v1/models and OpenAI-shaped errors (2 pts)

**Why** — SDKs probe models and parse error bodies.

**Do** — Models list from project settings; every error uses the OpenAI error envelope.

**Acceptance**
- [x] SDK models.list works
- [x] Errors parse in the SDK without exceptions of the wrong type

### ✔ SEM-15 · Conformance suite for OpenAI SDK and LangChain (5 pts)

**Why** — Compatibility is the product.

**Do** — Test suite driving both clients through text, stream, tools and errors in replay mode.

**Acceptance**
- [x] Suite runs in CI with no network
- [x] Failures name the incompatible field

## SEM-E4 · Projects, entries and safe edits (7/7)

**Goal** — Recorded answers are organised per project and can be edited without losing work.

**Done when** — Entries can be listed, edited, pinned and deleted with conflicts rejected, via REST.

**Not in scope** — UI.

### ✔ SEM-16 · Manage projects through the service and REST API (3 pts)

**Why** — Separation per project is a stated requirement.

**Do** — Create, list, update, delete projects; slug IDs; one service layer shared by all surfaces.

**Acceptance**
- [x] REST and CLI call the same functions
- [x] Deleting a project removes its entries

### ✔ SEM-17 · Route calls to a project by header or URL (2 pts)

**Why** — Apps must pick a project without code changes.

**Do** — X-Semula-Project header and /v1/projects/<id>/... path both resolve.

**Acceptance**
- [x] Both routes reach the same project
- [x] Unknown project returns a clear error

### ✔ SEM-18 · Per-project matching settings (3 pts)

**Why** — Apps differ in which fields matter.

**Do** — Settings for ignored fields, tail_n, scrub regexes, record_timeout, with defaults.

**Acceptance**
- [x] Setting change alters fingerprint behaviour in a test
- [x] Defaults documented

### ✔ SEM-19 · List, read, edit and delete entries with versions (5 pts)

**Why** — Two writers, one an agent, must not silently overwrite each other.

**Do** — Entry version field; writes carry expected version; stale write returns 409 with the current entry.

**Acceptance**
- [x] Stale write is rejected
- [x] Lean list projection returns no bodies

### ✔ SEM-20 · Pin entries against agent overwrite (2 pts)

**Why** — A human fix must survive a re-record.

**Do** — pinned flag; agent writes to a pinned entry fail unless forced.

**Acceptance**
- [x] Agent write to pinned entry returns 409
- [x] Force flag overrides and is logged

### ✔ SEM-21 · Select among several responses per fingerprint (3 pts)

**Why** — Some tests need the nth call to differ.

**Do** — Policies first, sequence, cycle per entry.

**Acceptance**
- [x] Sequence returns responses in order
- [x] Cycle wraps

### ✔ SEM-22 · Template dynamic fields at replay (2 pts)

**Why** — Verbatim replay breaks assertions on time and IDs.

**Do** — {{now}}, {{uuid}}, {{request.model}} expanded when replaying.

**Acceptance**
- [x] Two replays give different uuids
- [x] Unknown template is left intact

## SEM-E5 · Agent-operable: MCP, CLI and playbook (7/7)

**Goal** — Claude Code or Codex can set up projects, answer pending calls and fix entries without a human.

**Done when** — A coding agent completes a record session end to end using only MCP tools.

**Not in scope** — Spawning agents.

### ✔ SEM-23 · Serve MCP over streamable HTTP at /mcp (5 pts)

**Why** — Agents should configure semula natively.

**Do** — Tools: create_project, list_pending, answer_pending, list_entries, get_entry, upsert_entry, test_match, set_mode.

**Acceptance**
- [x] Claude Code connects and lists tools
- [x] Tools call the shared service layer

### ✔ SEM-24 · Provide a stdio shim: semula mcp (2 pts)

**Why** — Some clients only speak stdio.

**Do** — Command bridging stdio to the HTTP MCP endpoint.

**Acceptance**
- [x] Works in a .mcp.json entry
- [x] Exits non-zero when the server is down

### ✔ SEM-25 · Write tool descriptions that steer agent answers (1 pts)

**Why** — Agents must template dynamic fields and match the request's format.

**Do** — MCP tool descriptions say how to generalise timestamps and IDs and how to format tool calls.

**Acceptance**
- [x] Descriptions reviewed against D6 and D7 in QUESTIONS.md

### ✔ SEM-26 · Make the CLI machine-friendly (3 pts)

**Why** — Agents parse CLI output.

**Do** — --json everywhere, stable exit codes, errors on stderr.

**Acceptance**
- [x] Exit codes documented
- [x] Every command has --json

### ✔ SEM-27 · Add test_match: which entry would fire and why (3 pts)

**Why** — Agents and humans need to debug matching.

**Do** — CLI and MCP return tier, score, entry id and the nearest candidate.

**Acceptance**
- [x] Miss returns the nearest candidate and score
- [x] Hit returns the tier

### ✔ SEM-28 · Optional shared secret for all surfaces (2 pts)

**Why** — Containers may be exposed beyond loopback.

**Do** — SEMULA_API_KEY checked on /v1, /api and /mcp when set.

**Acceptance**
- [x] Missing key returns 401
- [x] Unset means open, documented

**Not in scope** — Users or roles.

### ✔ SEM-29 · Write the agent recording playbook (3 pts)

**Why** — Agents need a repeatable loop.

**Do** — Doc plus a Claude Code skill: poll pending, answer, verify, pin.

**Acceptance**
- [x] A fresh agent follows it to record a session
- [x] Covers Codex too

## SEM-E6 · Smarter matching with human review (4/4)

**Goal** — Near-miss prompts get answered without ever silently serving the wrong response.

**Done when** — BM25 hits above threshold auto-serve; weaker candidates queue for review with scores shown.

**Not in scope** — Embeddings.

### ✔ SEM-30 · Add configurable scrubbers to the normaliser (3 pts)

**Why** — Each app has its own noise.

**Do** — Per-project regex scrub rules applied before hashing and indexing.

**Acceptance**
- [x] Rule change applies to new calls
- [x] Invalid regex is rejected with a message

### ✔ SEM-31 · Add a BM25 tier on SQLite FTS5 (5 pts)

**Why** — Exact variable names and identifiers need lexical matching.

**Do** — Index normalised requests; score threshold per project; tier recorded in call_log.

**Acceptance**
- [x] Near-identical prompt hits above threshold
- [x] Below threshold falls through

### ✔ SEM-32 · Create a review queue for weak candidates (3 pts)

**Why** — Weak matches should not serve automatically (F4).

**Do** — Store candidate, score and request; call proceeds to miss handling.

**Acceptance**
- [x] Candidate appears with score
- [x] Call is not answered from the candidate

### ✔ SEM-33 · Approve or reject a candidate (3 pts)

**Why** — Humans teach semula which near-misses are fine.

**Do** — Approve creates an alias to the entry; reject suppresses that pairing.

**Acceptance**
- [x] Approved prompt hits next time
- [x] Rejected pair never reappears

## SEM-E7 · Observable, exportable, shippable container (6/6)

**Goal** — Operators can see what happened, take cassettes into git, and ship a small image.

**Done when** — Image under 350 MB, p95 replay under 15 ms, projects export and import losslessly.

**Not in scope** — Hosted deployment.

### ✔ SEM-34 · Record every call in the call log with retention (2 pts)

**Why** — You cannot debug what you did not log.

**Do** — Row per call: time, project, mode, tier, score, latency, entry id; prune at 30 days or 50k rows.

**Acceptance**
- [x] Retention enforced by a test
- [x] No Authorization header stored

### ✔ SEM-35 · Add an in-process event bus and SSE log tail (3 pts)

**Why** — Live views and CLI tail need a stream.

**Do** — Publish call events; GET /api/v1/log/stream; semula tail.

**Acceptance**
- [x] Tail shows a call within 1 s
- [x] Slow consumers do not block the proxy

### ✔ SEM-36 · Redact headers and listed patterns before storage (3 pts)

**Why** — Recorded prompts can contain secrets.

**Do** — Drop sensitive headers; apply configurable regex redaction to stored requests.

**Acceptance**
- [x] Listed pattern never appears in the database
- [x] Redaction does not alter the fingerprint of the live request

### ✔ SEM-37 · Export and import a project as files (5 pts)

**Why** — Cassettes should be committed to git and diffed.

**Do** — Directory of JSON/YAML with format: semula/1 and ULIDs preserved.

**Acceptance**
- [x] Round trip is lossless
- [x] Re-import is idempotent

### ✔ SEM-38 · Slim the image and document the volume (3 pts)

**Why** — Local tool must be quick to pull and run.

**Do** — Multi-stage build, non-root user, /data volume, compose example.

**Acceptance**
- [x] Image under 350 MB
- [x] Cold start under 2 s

### ✔ SEM-39 · Benchmark replay latency (3 pts)

**Why** — Success numbers must be measured.

**Do** — Benchmark script over a 1k-entry project; wire into CI as a budget.

**Acceptance**
- [x] p95 under 15 ms without embeddings
- [x] CI fails if the budget is exceeded

## SEM-E8 · Optional semantic matching (3/3)

**Goal** — Users who want paraphrase matching can opt in without bloating the default image.

**Done when** — With the extra installed, paraphrases appear in the review queue with cosine scores.

**Not in scope** — Auto-serving embedding matches by default.

### ✔ SEM-40 · Package embeddings as an optional extra (3 pts)

**Why** — Default image must stay small.

**Do** — fastembed + bge-small-en-v1.5 behind an extra; model fetched once and cached on the volume.

**Acceptance**
- [x] Default image lacks the model
- [x] Offline start works after first download

### ✔ SEM-41 · Add an embedding tier that feeds the review queue (5 pts)

**Why** — Paraphrases are the main hit-rate gain.

**Do** — Cosine search over stored vectors; candidates go to review, never auto-serve by default.

**Acceptance**
- [x] Paraphrase appears in review with a score
- [x] p95 under 40 ms on 1k entries

### ✔ SEM-42 · Allow an opt-in auto-serve threshold per project (2 pts)

**Why** — Some projects accept the risk.

**Do** — Per-project threshold, off by default, logged when used.

**Acceptance**
- [x] Default never auto-serves
- [x] Threshold use appears in call_log

## SEM-E9 · Dashboard design and indah requests (2/3)

**Goal** — A free-form dashboard design is saved in the repo and every gap in indah is a tracked request.

**Done when** — docs/design/ holds the design; the 15 enhancements are filed and prioritised by the indah team.

**Not in scope** — Building the dashboard.

### ✔ SEM-43 · Design the local dashboard free of indah limits (5 pts)

**Why** — The look should be chosen for the job, not for what indah does today.

**Do** — Screens: live log, pending queue, entries browser and editor, review queue, playground. Saved as an HTML artifact plus notes in docs/design/.

**Acceptance**
- [x] Design files committed
- [x] Each screen lists the indah enhancements it needs

### ✔ SEM-44 · Sketch the hosted dashboard additions (3 pts)

**Why** — Hosted adds login, tokens, workspaces.

**Do** — Extra screens and flows: sign-in, workspace switcher, token management, members.

**Acceptance**
- [x] Saved beside the local design
- [x] Marks what differs from local

### · SEM-45 · Hand the indah request prompt to the indah team (1 pts)

**Why** — The enhancements need a planning round on their side.

**Do** — Give docs/INDAH-REQUESTS-PROMPT.md to the indah coding agent and record the outcome.

**Acceptance**
- [ ] Indah cards exist for the agreed items
- [ ] Priorities recorded back here

## SEM-E10 · Dashboard on indah (MVP) (0/7)

**Goal** — Humans use the dashboard in the local container to watch, answer, review and fix.

**Done when** — All five screens work in the container at /ui against the admin API.

**Not in scope** — Auth, multi-user, hosted.

### · SEM-46 · Mount indah at /ui in the single-port app (3 pts)

**Why** — One port, one process.

**Do** — Mount the indah ASGI app; dashboard calls the same service layer.

**Acceptance**
- [ ] /ui loads in the container
- [ ] /v1 and /mcp unaffected

### · SEM-47 · Build the live request log screen (5 pts)

**Why** — See hits, misses and scores as they happen.

**Do** — Streaming table with detail pane; needs indah push and Table v2.

**Acceptance**
- [ ] New call appears in under 1 s
- [ ] Row opens request and response

### · SEM-48 · Build the pending queue screen (5 pts)

**Why** — Humans should be able to answer parked calls too.

**Do** — List pending, edit and submit a response, with validation errors shown.

**Acceptance**
- [ ] Submitting releases the held call
- [ ] Invalid body shows the reason

### · SEM-49 · Build the entries browser (5 pts)

**Why** — Find and inspect recorded answers.

**Do** — Project switcher, filter, sort, hit counts, pinned and fuzzy flags.

**Acceptance**
- [ ] Filter by text finds an entry
- [ ] Deep link opens an entry

### · SEM-50 · Build the entry editor (5 pts)

**Why** — Fix a bad recording by hand.

**Do** — JSON/text editing with version conflict handling and pin toggle.

**Acceptance**
- [ ] Stale edit shows a conflict
- [ ] Pin survives save

### · SEM-51 · Build the review queue screen (5 pts)

**Why** — Approve or reject near-miss matches.

**Do** — Diff of request against candidate, score, approve and reject.

**Acceptance**
- [ ] Approve makes the prompt hit
- [ ] Reject removes it from the queue

### · SEM-52 · Build the playground (3 pts)

**Why** — Test a prompt and see why it matched.

**Do** — Prompt box calling test_match; shows tier, score, response.

**Acceptance**
- [ ] Shows tier and score
- [ ] Handles a miss

## SEM-E11 · Hosted readiness (design only) (0/3)

**Goal** — Hosted choices are written down so the local build does not foreclose them.

**Done when** — ADRs exist for hosted auth, tenancy and the indah-or-Svelte decision.

**Not in scope** — Any hosted implementation.

### · SEM-53 · Write the hosted auth and tokens ADR (3 pts)

**Why** — Dashboard login and API tokens are different problems.

**Do** — OIDC for people, per-project API tokens for /v1 and /mcp, scopes, rotation, audit.

**Acceptance**
- [ ] Threat model section included
- [ ] Maps each requirement to H-items in the indah request list

### · SEM-54 · Write the workspace tenancy ADR (2 pts)

**Why** — workspace_id is in the schema; its rules are not.

**Do** — Isolation rules, quotas, sharing and deletion.

**Acceptance**
- [ ] Every table's tenant rule listed

### · SEM-55 · Spike: indah versus Svelte + Vite + TypeScript (3 pts)

**Why** — The choice is deferred until hosting.

**Do** — Score both against the hosted screens once indah H1-H7 status is known.

**Acceptance**
- [ ] Decision recorded as an ADR
- [ ] Includes migration cost of the local screens

## SEM-E12 · Database portability (SQLite and Postgres) (6/6)

**Goal** — The same code runs on SQLite locally and Postgres when hosted, with real migrations and CI proving both.

**Done when** — The full integration suite passes on SQLite and on Postgres in CI, and an existing SQLite file upgrades in place.

**Not in scope** — Hosted deployment itself, Postgres-specific full-text search (SEM-E6 designs the seam).

### ✔ SEM-56 · Define the schema with SQLAlchemy Core and an engine factory (3 pts)

**Why** — Raw sqlite3 SQL ties every query to SQLite; hosted needs Postgres.

**Do** — Table definitions in one module, an engine factory that reads SEMULA_DATABASE_URL (default: SQLite file in the data dir), SQLite pragmas applied per connection, Postgres via an optional extra.

**Acceptance**
- [x] The schema is defined once and builds on SQLite and Postgres
- [x] SQLite keeps WAL, foreign keys and busy timeout
- [x] Postgres driver is an optional extra, not a default dependency

**Not in scope… (truncated, 513 chars total — use --full to see complete body)

### ✔ SEM-57 · Move migrations to Alembic and adopt existing SQLite files (5 pts)

**Why** — Hand-rolled SQL migrations do not run on Postgres and cannot be diffed or reversed.

**Do** — Packaged Alembic baseline equal to today's schema, run at startup, and an adoption path that stamps databases created by the old runner.

**Acceptance**
- [x] A fresh database migrates to head on both backends
- [x] A file made by the old runner upgrades in place with its data intact
- [x] Migration scripts ship inside the wheel

### ✔ SEM-58 · Port the service's queries to Core with real transactions (5 pts)

**Why** — About 35 raw queries mix domain logic with SQLite-only SQL (strftime, LIKE case rules, ordering by timestamp).

**Do** — Rewrite every query with Core expressions; multi-statement operations run in one transaction; timestamps and ordering are portable.

**Acceptance**
- [x] No raw SQL strings remain in the service
- [x] Answering a pending request writes the entry and the pending row atomically
- [x] Text search is case-insensitive on both backends

**Not in scope** — Changing behaviour… (truncated, 501 chars total — use --full to see complete body)

### ✔ SEM-59 · Keep async handlers from blocking on the database (3 pts)

**Why** — Postgres round trips would stall the event loop if handlers call the database directly.

**Do** — Run service calls in a threadpool from async handlers; make the pending-answer wake-up thread-safe.

**Acceptance**
- [x] A slow query does not block /health
- [x] Releasing a held call from another thread works
- [x] Existing concurrency tests still pass

### ✔ SEM-60 · Run the integration suite against Postgres in CI (3 pts)

**Why** — Portability that is not tested rots.

**Do** — Add a CI job with a Postgres service container; tests get an isolated schema per test through one fixture.

**Acceptance**
- [x] The CI job runs the unit and integration suites on Postgres
- [x] A local make target runs them against a throwaway container
- [x] Failures name the backend

### ✔ SEM-61 · Record the decision: amend ADR-0004 and document database settings (1 pts)

**Why** — ADR-0004 says SQLite only and the service layer hides it, which was only half true.

**Do** — New ADR on SQLAlchemy Core + Alembic, the Postgres path, and the lexical-search seam SEM-E6 must respect; README documents SEMULA_DATABASE_URL.

**Acceptance**
- [x] ADR merged and linked from ADR-0004
- [x] README shows both backends
