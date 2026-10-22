# semula

**A fake LLM server that learns from your coding agent.** Point your OpenAI-compatible app at it. The first time a prompt is new, your Claude Code or Codex subscription writes the answer. Every run after that replays it instantly, offline, for free.

> **Status: pre-alpha.** Record-then-replay works end to end for chat completions, including streaming and tool calls, and the OpenAI SDK and LangChain pass the conformance suite. Projects, entry editing, pinning, an MCP server and a full CLI work too. Near-miss matching with a review queue, a call log with live tail, redaction, export/import and a slim container work too. Optional semantic matching (embeddings) is built; the dashboard has its live log so far (the pending queue, entries, review and playground are next). Follow progress on the [slices](docs/SLICES.md).

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
- A human dashboard to watch calls, answer pending ones and review near-misses (the live log is built; the rest follows).

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

### As a service on your laptop

[`deploy/laptop/`](deploy/laptop/README.md) runs the published image as an always-on semula that every project on the machine can share, on a fixed loopback port and, if you use Traefik, at `http://semula.localhost/ui/`.

### Record or replay, per project

Each project is either recording (a prompt with no stored answer is held for an agent) or replaying (it fails fast), and **a project's choice is kept across restarts**:

```bash
uv run semula mode                                  # the server default and every project's mode
uv run semula mode record --project support-bot     # kept
uv run semula mode record                           # only moves the server default, until a restart
```

A project that has not chosen follows the server default, which starts as `SEMULA_MODE` (`replay` unless you set it). The dashboard's Record switch sets the mode of the project you are looking at. A project's mode is never exported, and importing a bundle cannot turn recording on.

### Projects, editing and settings

Keep recordings apart per project and pick one per call with a header or the URL:

```bash
uv run semula project create my-app
curl -H 'X-Semula-Project: my-app' http://127.0.0.1:8080/v1/chat/completions ...   # or /v1/projects/my-app/chat/completions
```

Entries can hold several responses (`selection`: `first`, `sequence` or `cycle`), be edited with optimistic versions (a stale write gets a 409 with the current entry), and be pinned so an agent cannot overwrite a human fix. Responses may contain `{{now}}`, `{{uuid}}` and `{{request.model}}`, expanded fresh on every replay. Per-project matching settings (`tail_n`, `scrub_patterns`, `include_params`, `models`, `record_timeout`, `bm25_serve_threshold`, `bm25_review_threshold`) are set with `PATCH /api/v1/projects/<id>`; `GET` shows the defaults.

Matching is tiered and never serves a weak guess. A prompt that differs only by noise (timestamps, UUIDs, paths, your scrub rules) matches exactly. One that shares nearly all its words, with the same roles, tools and params, is served by a word-overlap tier (SQLite FTS5 / BM25 finds candidates; the score must reach `bm25_serve_threshold`; the default 1 means near-misses are reviewed, not served, until you lower it per project). A weaker near-miss is a normal miss, and the pairing waits in a review queue: `semula review list`, then `review approve <id>` (that prompt is then served from the entry) or `review reject <id>` (never paired again). The same is available over REST (`/api/v1/candidates`) and MCP.

### Let your coding agent drive it

semula serves MCP at `/mcp/` (streamable HTTP) and as a stdio server (`semula mcp`). With Claude Code:

```bash
claude mcp add --transport http semula http://127.0.0.1:8080/mcp/
```

The agent then lists parked calls, answers them, pre-records entries, edits and pins, and checks matches with `test_match`. The loop, answer formats and error handling are in the [agent playbook](docs/agents/PLAYBOOK.md); a ready-made Claude Code skill is in [`docs/agents/claude-code-skill/`](docs/agents/claude-code-skill/SKILL.md). Every CLI command takes `--json`; exit codes are `0` ok, `1` unreachable, `2` usage, `3` not found, `4` rejected, `5` authentication.

The dashboard is at `/ui` (the root redirects to the live log of the project with the latest call; it is built on the first visit). `make run` serves it; `make demo` serves it with demo data and a moving log, then throws the data away (`make demo ARGS=--record` parks two calls instead); `SEMULA_UI=off` hides it. To protect a server that is reachable beyond localhost, set `SEMULA_API_KEY`: the dashboard then switches itself off (a browser cannot send the key on a page load; sign-in is a hosted item, ADR-0010), and `/v1`, `/api` and `/mcp` require `Authorization: Bearer <key>` (or `X-API-Key`); `/health` stays open. The CLI reads the key from the same environment variable.

### Database

SQLite by default (a file in `SEMULA_DATA_DIR`, nothing to set up). For a shared or hosted setup point semula at Postgres:

```bash
uv sync --extra postgres
SEMULA_DATABASE_URL=postgresql://user:pass@host:5432/semula uv run semula serve
```

Migrations (Alembic) run at startup on both. A SQLite file made by an earlier semula is upgraded in place. In the image: `docker build --build-arg UV_EXTRAS="--extra postgres" .`. `make test-postgres` runs the whole suite against a throwaway Postgres container. See [ADR-0009](docs/adr/0009-sqlalchemy-core-alembic-postgres-path.md).

### Semantic matching (optional)

Paraphrases ("the screen is cracked" / "the display is shattered") share few words, so the word tiers miss them. Install the extra and switch it on: `uv sync --extra embeddings`, then `SEMULA_EMBEDDINGS=on semula serve`. The first start downloads a 65 MB model (bge-small-en-v1.5) into `<data dir>/models`; later starts need no network (`SEMULA_EMBEDDINGS_OFFLINE=on` forbids downloading, `semula embeddings fetch` pre-downloads). Paraphrases land in the review queue with their cosine score and are **never served unless you opt in** per project (`embedding_serve_threshold`). Embedding runs only after every cheaper tier has missed and never delays a hit or a write. `semula embeddings status` shows progress. With Docker: `docker compose --profile embeddings up -d --wait semula-embed` (a 440 MB image; the default stays at 240 MB and has no model).

### Container

```bash
docker compose up -d --wait semula                          # SQLite on a named volume
docker compose --profile postgres up -d --wait semula-pg    # the same app on Postgres
```

[`compose.yaml`](compose.yaml) is the reference. The image (about 240 MB, a non-root user, starts in under 2 s) keeps all state under `/data`: mount a volume there (`-v semula-data:/data`) and recordings survive upgrades and restarts. A bind mount must be writable by uid 10001 (`chown 10001 ./data`). With Postgres, `/data` is unused and the database holds everything. Set `SEMULA_API_KEY` before publishing the port beyond loopback. `make docker` runs a throwaway container on the first free port; `make image-check` enforces the size and cold-start budgets.

### Export and import

`semula export <project> --dir cassettes/` writes one JSON file per entry (ULIDs kept, sorted keys, so git diffs are clean) plus approved aliases; `semula import cassettes/` loads them into any server, creating the project if needed. Import matches by id, updates what changed, skips what did not, and never deletes, so it is safe to repeat (pinned entries are left alone without `--force`). Runtime state (hit counts, the call log, the review queue) is not exported. Stored requests are redacted (API keys and similar by default, plus `redact_patterns` per project), so the files are safe to commit; recorded *answers* are not redacted, so read them before you push.

### Watching calls

`semula tail` follows calls live (`--json` for one object per line), `semula log` lists recent ones (`--stats` gives per-minute counts, hit rate, median latency and how many calls are parked), and `GET /api/v1/log/stream` is the same feed as server-sent events. The log keeps 30 days or 50,000 rows (`SEMULA_LOG_RETENTION_DAYS`, `SEMULA_LOG_MAX_ROWS`) and never stores request headers. Each row carries a redacted preview (120 characters) of the prompt.

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
