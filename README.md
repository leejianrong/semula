# semula

**A fake LLM server that learns from your coding agent.** Point your OpenAI-compatible app at it. The first time a prompt is new, your Claude Code or Codex subscription writes the answer. Every run after that replays it instantly, offline, for free.

> **Status: pre-alpha.** Record-then-replay works end to end for chat completions, including streaming and tool calls, and the OpenAI SDK and LangChain pass the conformance suite. Projects, entry editing, pinning, an MCP server and a full CLI work too. Fuzzy matching and the dashboard are still to come. Follow progress on the [slices](docs/SLICES.md).

```mermaid
flowchart LR
    App["Your app / tests<br/>(OpenAI SDK)"] -->|/v1/chat/completions| S["semula"]
    S -->|hit: replay| App
    S -.->|miss, record mode: park| Q[("pending queue")]
    Agent["Claude Code / Codex<br/>(MCP or CLI)"] -->|list + answer| Q
    Q -->|validated answer saved| DB[("SQLite<br/>per-project entries")]
    S --- DB
```

## Why

Development and test runs call the same prompts over and over, and a paid API bills every one. Hand-written mocks break the moment a framework adds a timestamp or reorders a tool list. semula normalises that noise, matches tolerantly, and never serves a weak guess silently: near-misses go to a review queue.

## What it will do

- Speak the OpenAI Chat Completions API: text, streaming and tool calls.
- Record mode: an unknown prompt is parked, a coding agent answers it over MCP or CLI, semula validates and saves it.
- Replay mode: known prompts return the stored answer with no network. Target: p95 under 15 ms.
- Organise recordings by project; export them to files you can commit and diff.
- A human dashboard to watch calls, answer pending ones and review near-misses (after the core).

## Try it

From a clone (needs [uv](https://docs.astral.sh/uv/)). Not on PyPI yet.

```bash
git clone https://github.com/leejianrong/semula && cd semula
uv sync
SEMULA_MODE=record uv run semula serve        # terminal 1, listens on 127.0.0.1:8080
```

Point any OpenAI-compatible client at `http://127.0.0.1:8080/v1`. The first call for a new prompt is held. In a second terminal, answer it as your coding agent would:

```bash
uv run semula pending                          # id, status, project, last message
uv run semula answer <id> --text "hi from the agent"
```

The held call returns. Ask the same thing again, even with a different model name or a new timestamp in the prompt, and the stored answer comes back at once. Restart with `SEMULA_MODE=replay` and it works offline; unknown prompts return an error instead of a made-up answer.

### Projects, editing and settings

Keep recordings apart per project and pick one per call with a header or the URL:

```bash
uv run semula project create my-app
curl -H 'X-Semula-Project: my-app' http://127.0.0.1:8080/v1/chat/completions ...   # or /v1/projects/my-app/chat/completions
```

Entries can hold several responses (`selection`: `first`, `sequence` or `cycle`), be edited with optimistic versions (a stale write gets a 409 with the current entry), and be pinned so an agent cannot overwrite a human fix. Responses may contain `{{now}}`, `{{uuid}}` and `{{request.model}}`, expanded fresh on every replay. Per-project matching settings (`tail_n`, `scrub_patterns`, `include_params`, `models`, `record_timeout`, `bm25_serve_threshold`, `bm25_review_threshold`) are set with `PATCH /api/v1/projects/<id>`; `GET` shows the defaults.

Matching is tiered and never serves a weak guess. A prompt that differs only by noise (timestamps, UUIDs, paths, your scrub rules) matches exactly. One that shares nearly all its words, with the same roles, tools and params, is served by a word-overlap tier (SQLite FTS5 / BM25 finds candidates; the score must reach `bm25_serve_threshold`, default 0.97). A weaker near-miss is a normal miss, and the pairing waits in a review queue: `semula review list`, then `review approve <id>` (that prompt is then served from the entry) or `review reject <id>` (never paired again). The same is available over REST (`/api/v1/candidates`) and MCP.

### Let your coding agent drive it

semula serves MCP at `/mcp/` (streamable HTTP) and as a stdio server (`semula mcp`). With Claude Code:

```bash
claude mcp add --transport http semula http://127.0.0.1:8080/mcp/
```

The agent then lists parked calls, answers them, pre-records entries, edits and pins, and checks matches with `test_match`. The loop, answer formats and error handling are in the [agent playbook](docs/agents/PLAYBOOK.md); a ready-made Claude Code skill is in [`docs/agents/claude-code-skill/`](docs/agents/claude-code-skill/SKILL.md). Every CLI command takes `--json`; exit codes are `0` ok, `1` unreachable, `2` usage, `3` not found, `4` rejected, `5` authentication.

To protect a server that is reachable beyond localhost, set `SEMULA_API_KEY`. `/v1`, `/api` and `/mcp` then require `Authorization: Bearer <key>` (or `X-API-Key`); `/health` stays open. The CLI reads the key from the same environment variable.

### Database

SQLite by default (a file in `SEMULA_DATA_DIR`, nothing to set up). For a shared or hosted setup point semula at Postgres:

```bash
uv sync --extra postgres
SEMULA_DATABASE_URL=postgresql://user:pass@host:5432/semula uv run semula serve
```

Migrations (Alembic) run at startup on both. A SQLite file made by an earlier semula is upgraded in place. In the image: `docker build --build-arg UV_EXTRAS="--extra postgres" .`. `make test-postgres` runs the whole suite against a throwaway Postgres container. See [ADR-0009](docs/adr/0009-sqlalchemy-core-alembic-postgres-path.md).

Or run the container: `make docker` (Docker; picks the first free port from 8080).

## Documents

| | |
|---|---|
| [`PLAN.md`](docs/PLAN.md) | Problem, scope, requirements, architecture |
| [`SLICES.md`](docs/SLICES.md) | Build increments with acceptance checks |
| [`QUESTIONS.md`](docs/QUESTIONS.md) | Decision register |
| [`docs/adr/`](docs/adr/) | Architecture decisions |
| [`docs/INDAH-REQUESTS-PROMPT.md`](docs/INDAH-REQUESTS-PROMPT.md) | What the dashboard needs from [indah](https://github.com/leejianrong/indah) |

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Agents: read [`AGENTS.md`](AGENTS.md).

## License

[Apache License 2.0](LICENSE).
