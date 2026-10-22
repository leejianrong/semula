# semula

**A fake LLM server that learns from your coding agent.** Point your OpenAI-compatible app at it. The first time a prompt is new, your Claude Code or Codex subscription writes the answer. Every run after that replays it instantly, offline, for free.

![The Pending screen: parked calls, one selected, an answer typed in](docs/assets/dashboard-pending.png)

> **0.x.** semula works end to end and the tests are broad, but a minor release may still change behaviour. The [changelog](CHANGELOG.md) lists every change, and recordings made with one release open in the next.

## Why

Development and test runs call the same prompts over and over, and a paid API bills every one. Hand-written mocks break the moment a framework adds a timestamp or reorders a tool list. semula normalises that noise, matches tolerantly, and never serves a weak guess silently: near misses go to a review queue for you.

## Quick start

You need Docker. Start semula in record mode:

```bash
docker run -d --name semula -p 127.0.0.1:8080:8080 \
  -e SEMULA_MODE=record -v semula-data:/data ghcr.io/leejianrong/semula
```

Point any OpenAI-compatible client at `http://127.0.0.1:8080/v1`. The first call for a new prompt is held:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Name a fruit."}]}'
```

In a second terminal, answer it as your coding agent would:

```bash
docker exec semula semula pending
docker exec semula semula answer <id> --text "Mango"
```

The held call returns `Mango`. Ask again and the stored answer comes back in a few milliseconds, even with a different model name or a new timestamp in the prompt. Open <http://127.0.0.1:8080/ui/> to watch the calls, and answer parked ones from the **Pending** screen.

Switch a project to replay and it works offline; a prompt nobody recorded fails with `424` instead of inventing an answer:

```bash
docker exec semula semula mode replay --project default
```

## Let your coding agent answer

semula speaks MCP over HTTP at `/mcp/`, so there is nothing to install:

```bash
claude mcp add --transport http semula http://127.0.0.1:8080/mcp/      # Claude Code
codex mcp add semula --url http://127.0.0.1:8080/mcp/                  # Codex
```

The agent calls `wait_for_pending`, reads the request, and calls `answer_pending`. Tell it to do that in a loop and your app's calls are answered as they arrive. The [agent playbook](docs/agents/PLAYBOOK.md) covers answer formats, tool calls and errors, and `docs/agents/claude-code-skill/` is a ready-made Claude Code skill. Both agents were run against the container to check this.

No MCP? The agent can use the CLI the same way you did: `docker exec semula semula pending --wait 60 --json`.

### The CLI without Docker

Each [release](https://github.com/leejianrong/semula/releases/latest) has the CLI as a single file for Linux and macOS (amd64 and arm64), with checksums. It talks to a running server:

```bash
chmod +x semula-*-linux-amd64 && mv semula-*-linux-amd64 ~/.local/bin/semula
semula --url http://127.0.0.1:8080 pending
```

## What you get

- The OpenAI Chat Completions API: text, streaming and tool calls. The OpenAI SDK and LangChain pass the conformance suite.
- Record mode that parks unknown prompts, and replay mode that fails fast. Mode is set per project and kept across restarts.
- Tiered matching: exact, normalised (timestamps, ids and paths ignored), approved aliases, word overlap, and optional meaning-based matching. Near misses wait in a review queue; a weak guess is never served by default.
- Projects, entries with several answers, templates (`{{now}}`, `{{uuid}}`), versions with conflict detection, and pins that keep an agent from overwriting a human fix.
- Redaction of secrets in what is stored; export and import as JSON files you can commit and diff.
- A call log with a live tail, per-minute stats and a dashboard. SQLite by default, Postgres when you share it. Replay p95 is under 15 ms.

## Documentation

Full docs, with a tutorial: **<https://leejianrong.github.io/semula/>**

| | |
|---|---|
| [Tutorial](https://leejianrong.github.io/semula/tutorial/first-recording/) | Nine short pages, from the first recording to the dashboard |
| [Install](https://leejianrong.github.io/semula/guide/install/) | Docker, the CLI binary, the Python package |
| [Run it as a service](deploy/laptop/README.md) | One always-on semula for every project on your machine |
| [Upgrade and back up](https://leejianrong.github.io/semula/guide/upgrade-and-backup/) | What is stored, upgrading, backing up |
| [Reference](https://leejianrong.github.io/semula/reference/cli/) | CLI, MCP tools, REST, settings, errors |
| [`docs/adr/`](docs/adr/) | Why it is built the way it is |

## Contributing

From a clone (needs [uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/leejianrong/semula && cd semula
uv sync --extra dev && make hooks
SEMULA_MODE=record uv run semula serve      # or: make run, make demo
make check                                  # lint, types, fast tests
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Agents: read [`AGENTS.md`](AGENTS.md).

## License

[Apache License 2.0](LICENSE).
