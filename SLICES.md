# semula: Slices

Vertical increments, headless first. The live source of status is Pandan board **SEM** (board 39); this file is the plan-time snapshot. Slice V1 confronts the riskiest mechanism: parking a call and answering it from a coding agent.

Decisions behind these: see `QUESTIONS.md` (F1-F4 decided).

## SEM-E1 · Plan written down

**Goal:** The plan, ADRs and slices exist in the repo so any agent can build from them.

**Done when:** PLAN.md, SLICES.md, QUESTIONS.md and the load-bearing ADRs are committed and consistent.

**Not in scope:** Implementation.

### SEM-1 · Write PLAN.md from the decision register (3 pts)

**Why:** The register (QUESTIONS.md) holds decisions but no narrative an agent can build from.

**Do:** PLAN.md with problem, users, scope, requirements, shape, indah readiness, success numbers, out of scope.

**Test plan**
- PLAN.md cites ADRs by number
- Every requirement maps to a slice in SLICES.md

### SEM-2 · Write the load-bearing ADRs (3 pts)

**Why:** Future agents would re-litigate stack, matching, record queue and storage choices.

**Do:** One ADR each: Python/FastAPI, record queue (F1), tiered matching (F4), SQLite + workspace_id (F3), single-process runtime.

**Test plan**
- Each ADR lists rejected alternatives
- ADR numbers are referenced from PLAN.md

## SEM-E2 · Record once, replay forever (walking skeleton)

**Goal:** An OpenAI SDK call is parked, answered by a coding agent, saved, and replayed instantly with no network.

**Done when:** With outbound network blocked, the second identical call returns the recorded answer in under 15 ms.

**Not in scope:** Streaming, tools, fuzzy matching, dashboard.

### SEM-3 · Scaffold the service and container (2 pts)

**Why:** Nothing can be built or run until a service boots in a container.

**Do:** FastAPI app, config via env, /health, Dockerfile, uv project, make targets for check.

**Test plan**
- docker run serves /health on :8080
- make check runs lint and tests

### SEM-4 · Create the SQLite store with migrations (3 pts)

**Why:** Projects, entries and the call log need a stable schema with a path to change it.

**Do:** Migration runner plus tables: workspace, project, entry, call_log, pending. workspace_id on every table.

**Test plan**
- schema_version table exists
- Migrations apply to an empty file and are idempotent

### SEM-5 · Normalise a request and compute its fingerprint (3 pts)

**Why:** Frameworks add timestamps, UUIDs and paths, so raw hashing misses.

**Do:** Canonical request type, scrubbers for time, UUID and absolute paths, tools sorted by name, stable hash. Ignore model, temperature, stream by default.

**Test plan**
- Two requests differing only in a timestamp share a fingerprint
- Reordered tools share a fingerprint

### SEM-6 · Replay a recorded answer on /v1/chat/completions (3 pts)

**Why:** The core value: a client points at semula and gets an answer for free.

**Do:** Non-streaming endpoint; exact fingerprint hit returns the stored OpenAI-shaped response and bumps hit_count.

**Test plan**
- OpenAI Python SDK gets the stored answer
- A miss in replay mode returns a semula_no_match error with HTTP 4xx

### SEM-7 · Park a miss as pending in record mode (5 pts)

**Why:** Record mode must hold the client call until an agent answers; this is the riskiest mechanism.

**Do:** On miss in record mode, store a pending row and wait up to record_timeout for an answer; late answers are still saved.

**Test plan**
- A held call returns once an answer is submitted
- A timed-out call returns semula_record_timeout and the pending row remains

### SEM-8 · List and answer pending requests from the CLI (3 pts)

**Why:** The agent needs a way to see parked requests and submit responses.

**Do:** semula pending and semula answer <id> --file/--stdin, with --json and exit codes.

**Test plan**
- pending lists the parked request
- answer releases the held HTTP call

### SEM-9 · Validate submitted responses against the OpenAI schema (3 pts)

**Why:** A malformed answer would be saved and break every replay.

**Do:** Reject invalid chat completion bodies with a message the agent can act on.

**Test plan**
- Invalid body is rejected, not saved
- Valid body is saved and replayed

### SEM-10 · Demo: record then replay with network blocked (2 pts)

**Why:** Proves the claim of zero API cost in replay.

**Do:** Script that records one call with an agent, then replays it with outbound network denied.

**Test plan**
- Second call succeeds offline
- Recorded in docs as the acceptance demo

## SEM-E3 · Streaming and tool calls that real SDKs accept

**Goal:** Streaming and tool-calling clients work unchanged against semula.

**Done when:** OpenAI SDK and LangChain pass a conformance suite for streaming, tools and errors.

**Not in scope:** Responses API, Anthropic Messages.

### SEM-11 · Replay streaming responses as SSE (3 pts)

**Why:** Most apps set stream: true.

**Do:** Re-chunk the stored message (default 24 chars) into SSE ending with [DONE]; usage and finish_reason correct.

**Test plan**
- SDK stream iterator yields the full text
- Chunk size is configurable

### SEM-12 · Capture real stream chunk boundaries when recorded (3 pts)

**Why:** Faithful replay of a recorded stream needs its chunk boundaries.

**Do:** Store chunk boundaries when an answer arrives as a stream; replay them verbatim.

**Test plan**
- Replayed chunks equal recorded chunks
- Falls back to re-chunking when none stored

### SEM-13 · Validate tool_calls against the request's tool schemas (3 pts)

**Why:** Agent-written tool calls can violate the app's schemas.

**Do:** Check function names and argument JSON against the request tools before saving.

**Test plan**
- Unknown tool name is rejected
- Arguments failing the schema are rejected with the path

### SEM-14 · Serve /v1/models and OpenAI-shaped errors (2 pts)

**Why:** SDKs probe models and parse error bodies.

**Do:** Models list from project settings; every error uses the OpenAI error envelope.

**Test plan**
- SDK models.list works
- Errors parse in the SDK without exceptions of the wrong type

### SEM-15 · Conformance suite for OpenAI SDK and LangChain (5 pts)

**Why:** Compatibility is the product.

**Do:** Test suite driving both clients through text, stream, tools and errors in replay mode.

**Test plan**
- Suite runs in CI with no network
- Failures name the incompatible field

## SEM-E4 · Projects, entries and safe edits

**Goal:** Recorded answers are organised per project and can be edited without losing work.

**Done when:** Entries can be listed, edited, pinned and deleted with conflicts rejected, via REST.

**Not in scope:** UI.

### SEM-16 · Manage projects through the service and REST API (3 pts)

**Why:** Separation per project is a stated requirement.

**Do:** Create, list, update, delete projects; slug IDs; one service layer shared by all surfaces.

**Test plan**
- REST and CLI call the same functions
- Deleting a project removes its entries

### SEM-17 · Route calls to a project by header or URL (2 pts)

**Why:** Apps must pick a project without code changes.

**Do:** X-Semula-Project header and /v1/projects/<id>/... path both resolve.

**Test plan**
- Both routes reach the same project
- Unknown project returns a clear error

### SEM-18 · Per-project matching settings (3 pts)

**Why:** Apps differ in which fields matter.

**Do:** Settings for ignored fields, tail_n, scrub regexes, record_timeout, with defaults.

**Test plan**
- Setting change alters fingerprint behaviour in a test
- Defaults documented

### SEM-19 · List, read, edit and delete entries with versions (5 pts)

**Why:** Two writers, one an agent, must not silently overwrite each other.

**Do:** Entry version field; writes carry expected version; stale write returns 409 with the current entry.

**Test plan**
- Stale write is rejected
- Lean list projection returns no bodies

### SEM-20 · Pin entries against agent overwrite (2 pts)

**Why:** A human fix must survive a re-record.

**Do:** pinned flag; agent writes to a pinned entry fail unless forced.

**Test plan**
- Agent write to pinned entry returns 409
- Force flag overrides and is logged

### SEM-21 · Select among several responses per fingerprint (3 pts)

**Why:** Some tests need the nth call to differ.

**Do:** Policies first, sequence, cycle per entry.

**Test plan**
- Sequence returns responses in order
- Cycle wraps

### SEM-22 · Template dynamic fields at replay (2 pts)

**Why:** Verbatim replay breaks assertions on time and IDs.

**Do:** {{now}}, {{uuid}}, {{request.model}} expanded when replaying.

**Test plan**
- Two replays give different uuids
- Unknown template is left intact

## SEM-E5 · Agent-operable: MCP, CLI and playbook

**Goal:** Claude Code or Codex can set up projects, answer pending calls and fix entries without a human.

**Done when:** A coding agent completes a record session end to end using only MCP tools.

**Not in scope:** Spawning agents.

### SEM-23 · Serve MCP over streamable HTTP at /mcp (5 pts)

**Why:** Agents should configure semula natively.

**Do:** Tools: create_project, list_pending, answer_pending, list_entries, get_entry, upsert_entry, test_match, set_mode.

**Test plan**
- Claude Code connects and lists tools
- Tools call the shared service layer

### SEM-24 · Provide a stdio shim: semula mcp (2 pts)

**Why:** Some clients only speak stdio.

**Do:** Command bridging stdio to the HTTP MCP endpoint.

**Test plan**
- Works in a .mcp.json entry
- Exits non-zero when the server is down

### SEM-25 · Write tool descriptions that steer agent answers (1 pts)

**Why:** Agents must template dynamic fields and match the request's format.

**Do:** MCP tool descriptions say how to generalise timestamps and IDs and how to format tool calls.

**Test plan**
- Descriptions reviewed against D6 and D7 in QUESTIONS.md

### SEM-26 · Make the CLI machine-friendly (3 pts)

**Why:** Agents parse CLI output.

**Do:** --json everywhere, stable exit codes, errors on stderr.

**Test plan**
- Exit codes documented
- Every command has --json

### SEM-27 · Add test_match: which entry would fire and why (3 pts)

**Why:** Agents and humans need to debug matching.

**Do:** CLI and MCP return tier, score, entry id and the nearest candidate.

**Test plan**
- Miss returns the nearest candidate and score
- Hit returns the tier

### SEM-28 · Optional shared secret for all surfaces (2 pts)

**Why:** Containers may be exposed beyond loopback.

**Do:** SEMULA_API_KEY checked on /v1, /api and /mcp when set.

**Test plan**
- Missing key returns 401
- Unset means open, documented

### SEM-29 · Write the agent recording playbook (3 pts)

**Why:** Agents need a repeatable loop.

**Do:** Doc plus a Claude Code skill: poll pending, answer, verify, pin.

**Test plan**
- A fresh agent follows it to record a session
- Covers Codex too

## SEM-E6 · Smarter matching with human review

**Goal:** Near-miss prompts get answered without ever silently serving the wrong response.

**Done when:** BM25 hits above threshold auto-serve; weaker candidates queue for review with scores shown.

**Not in scope:** Embeddings.

### SEM-30 · Add configurable scrubbers to the normaliser (3 pts)

**Why:** Each app has its own noise.

**Do:** Per-project regex scrub rules applied before hashing and indexing.

**Test plan**
- Rule change applies to new calls
- Invalid regex is rejected with a message

### SEM-31 · Add a BM25 tier on SQLite FTS5 (5 pts)

**Why:** Exact variable names and identifiers need lexical matching.

**Do:** Index normalised requests; score threshold per project; tier recorded in call_log.

**Test plan**
- Near-identical prompt hits above threshold
- Below threshold falls through

### SEM-32 · Create a review queue for weak candidates (3 pts)

**Why:** Weak matches should not serve automatically (F4).

**Do:** Store candidate, score and request; call proceeds to miss handling.

**Test plan**
- Candidate appears with score
- Call is not answered from the candidate

### SEM-33 · Approve or reject a candidate (3 pts)

**Why:** Humans teach semula which near-misses are fine.

**Do:** Approve creates an alias to the entry; reject suppresses that pairing.

**Test plan**
- Approved prompt hits next time
- Rejected pair never reappears

## SEM-E7 · Observable, exportable, shippable container

**Goal:** Operators can see what happened, take cassettes into git, and ship a small image.

**Done when:** Image under 350 MB, p95 replay under 15 ms, projects export and import losslessly.

**Not in scope:** Hosted deployment.

### SEM-34 · Record every call in the call log with retention (2 pts)

**Why:** You cannot debug what you did not log.

**Do:** Row per call: time, project, mode, tier, score, latency, entry id; prune at 30 days or 50k rows.

**Test plan**
- Retention enforced by a test
- No Authorization header stored

### SEM-35 · Add an in-process event bus and SSE log tail (3 pts)

**Why:** Live views and CLI tail need a stream.

**Do:** Publish call events; GET /api/v1/log/stream; semula tail.

**Test plan**
- Tail shows a call within 1 s
- Slow consumers do not block the proxy

### SEM-36 · Redact headers and listed patterns before storage (3 pts)

**Why:** Recorded prompts can contain secrets.

**Do:** Drop sensitive headers; apply configurable regex redaction to stored requests.

**Test plan**
- Listed pattern never appears in the database
- Redaction does not alter the fingerprint of the live request

### SEM-37 · Export and import a project as files (5 pts)

**Why:** Cassettes should be committed to git and diffed.

**Do:** Directory of JSON/YAML with format: semula/1 and ULIDs preserved.

**Test plan**
- Round trip is lossless
- Re-import is idempotent

### SEM-38 · Slim the image and document the volume (3 pts)

**Why:** Local tool must be quick to pull and run.

**Do:** Multi-stage build, non-root user, /data volume, compose example.

**Test plan**
- Image under 350 MB
- Cold start under 2 s

### SEM-39 · Benchmark replay latency (3 pts)

**Why:** Success numbers must be measured.

**Do:** Benchmark script over a 1k-entry project; wire into CI as a budget.

**Test plan**
- p95 under 15 ms without embeddings
- CI fails if the budget is exceeded

## SEM-E8 · Optional semantic matching

**Goal:** Users who want paraphrase matching can opt in without bloating the default image.

**Done when:** With the extra installed, paraphrases appear in the review queue with cosine scores.

**Not in scope:** Auto-serving embedding matches by default.

### SEM-40 · Package embeddings as an optional extra (3 pts)

**Why:** Default image must stay small.

**Do:** fastembed + bge-small-en-v1.5 behind an extra; model fetched once and cached on the volume.

**Test plan**
- Default image lacks the model
- Offline start works after first download

### SEM-41 · Add an embedding tier that feeds the review queue (5 pts)

**Why:** Paraphrases are the main hit-rate gain.

**Do:** Cosine search over stored vectors; candidates go to review, never auto-serve by default.

**Test plan**
- Paraphrase appears in review with a score
- p95 under 40 ms on 1k entries

### SEM-42 · Allow an opt-in auto-serve threshold per project (2 pts)

**Why:** Some projects accept the risk.

**Do:** Per-project threshold, off by default, logged when used.

**Test plan**
- Default never auto-serves
- Threshold use appears in call_log

## SEM-E9 · Dashboard design and indah requests

**Goal:** A free-form dashboard design is saved in the repo and every gap in indah is a tracked request.

**Done when:** docs/design/ holds the design; the 15 enhancements are filed and prioritised by the indah team.

**Not in scope:** Building the dashboard.

### SEM-43 · Design the local dashboard free of indah limits (5 pts)

**Why:** The look should be chosen for the job, not for what indah does today.

**Do:** Screens: live log, pending queue, entries browser and editor, review queue, playground. Saved as an HTML artifact plus notes in docs/design/.

**Test plan**
- Design files committed
- Each screen lists the indah enhancements it needs

### SEM-44 · Sketch the hosted dashboard additions (3 pts)

**Why:** Hosted adds login, tokens, workspaces.

**Do:** Extra screens and flows: sign-in, workspace switcher, token management, members.

**Test plan**
- Saved beside the local design
- Marks what differs from local

### SEM-45 · Hand the indah request prompt to the indah team (1 pts)

**Why:** The enhancements need a planning round on their side.

**Do:** Give docs/INDAH-REQUESTS-PROMPT.md to the indah coding agent and record the outcome.

**Test plan**
- Indah cards exist for the agreed items
- Priorities recorded back here

## SEM-E10 · Dashboard on indah (MVP)

**Goal:** Humans use the dashboard in the local container to watch, answer, review and fix.

**Done when:** All five screens work in the container at /ui against the admin API.

**Not in scope:** Auth, multi-user, hosted.

### SEM-46 · Mount indah at /ui in the single-port app (3 pts)

**Why:** One port, one process.

**Do:** Mount the indah ASGI app; dashboard calls the same service layer.

**Test plan**
- /ui loads in the container
- /v1 and /mcp unaffected

### SEM-47 · Build the live request log screen (5 pts)

**Why:** See hits, misses and scores as they happen.

**Do:** Streaming table with detail pane; needs indah push and Table v2.

**Test plan**
- New call appears in under 1 s
- Row opens request and response

### SEM-48 · Build the pending queue screen (5 pts)

**Why:** Humans should be able to answer parked calls too.

**Do:** List pending, edit and submit a response, with validation errors shown.

**Test plan**
- Submitting releases the held call
- Invalid body shows the reason

### SEM-49 · Build the entries browser (5 pts)

**Why:** Find and inspect recorded answers.

**Do:** Project switcher, filter, sort, hit counts, pinned and fuzzy flags.

**Test plan**
- Filter by text finds an entry
- Deep link opens an entry

### SEM-50 · Build the entry editor (5 pts)

**Why:** Fix a bad recording by hand.

**Do:** JSON/text editing with version conflict handling and pin toggle.

**Test plan**
- Stale edit shows a conflict
- Pin survives save

### SEM-51 · Build the review queue screen (5 pts)

**Why:** Approve or reject near-miss matches.

**Do:** Diff of request against candidate, score, approve and reject.

**Test plan**
- Approve makes the prompt hit
- Reject removes it from the queue

### SEM-52 · Build the playground (3 pts)

**Why:** Test a prompt and see why it matched.

**Do:** Prompt box calling test_match; shows tier, score, response.

**Test plan**
- Shows tier and score
- Handles a miss

## SEM-E11 · Hosted readiness (design only)

**Goal:** Hosted choices are written down so the local build does not foreclose them.

**Done when:** ADRs exist for hosted auth, tenancy and the indah-or-Svelte decision.

**Not in scope:** Any hosted implementation.

### SEM-53 · Write the hosted auth and tokens ADR (3 pts)

**Why:** Dashboard login and API tokens are different problems.

**Do:** OIDC for people, per-project API tokens for /v1 and /mcp, scopes, rotation, audit.

**Test plan**
- Threat model section included
- Maps each requirement to H-items in the indah request list

### SEM-54 · Write the workspace tenancy ADR (2 pts)

**Why:** workspace_id is in the schema; its rules are not.

**Do:** Isolation rules, quotas, sharing and deletion.

**Test plan**
- Every table's tenant rule listed

### SEM-55 · Spike: indah versus Svelte + Vite + TypeScript (3 pts)

**Why:** The choice is deferred until hosting.

**Do:** Score both against the hosted screens once indah H1-H7 status is known.

**Test plan**
- Decision recorded as an ADR
- Includes migration cost of the local screens
