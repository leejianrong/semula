# semula

**A fake LLM server that learns from your coding agent.** Point your OpenAI-compatible app at it. The first time a prompt is new, your Claude Code or Codex subscription writes the answer. Every run after that replays it instantly, offline, for free.

> **Status: pre-alpha.** The plan and architecture are written; the code is just starting. Nothing here runs yet, so there is no install step. Follow progress on the [slices](docs/SLICES.md).

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

The intended shape of use, once it exists (not runnable today):

```bash
docker run -p 8080:8080 -v ./data:/data semula   # planned
export OPENAI_BASE_URL=http://localhost:8080/v1
```

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
