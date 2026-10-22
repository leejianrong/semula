# Changelog

All notable changes to semula are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). semula is 0.x: a minor release may change
behaviour, and each such change is listed under "Changed". Recordings made with one release
open in the next; the database upgrades itself on start and cannot be downgraded.

## [Unreleased]

The first public release. semula is a fake OpenAI-compatible server for development and tests:
unknown prompts are held for a coding agent (or you) to answer, and every later identical
prompt replays that answer for free.

### Added

- **Record and replay** for `/v1/chat/completions` (JSON and streaming, tool calls) and
  `/v1/models`. In record mode a prompt with no stored answer is held; answering it (MCP, CLI,
  REST) saves it and releases the call. In replay mode it fails fast, so a test never invents an
  answer.
- **Matching in tiers.** Exact, then normalised (timestamps, ids and paths ignored, configurable
  per project), then approved aliases, then word overlap. Near misses go to a review queue and are
  never served until a human approves them. An optional embedding tier (the `embeddings` extra)
  finds paraphrases and also goes to review.
- **Projects and entries.** Several answers per prompt (first, sequence, cycle), templates in
  answers (`{{now}}`, `{{uuid}}`), pins, versioned edits with conflict detection, and a history of
  the last 50 versions of each entry. List filters, sort and flags.
- **Waiting for work.** The MCP tool `wait_for_pending`, `semula pending --wait SECONDS` and
  `GET /api/v1/pending?wait=` return the moment a call is parked (or at once if some wait), and
  empty at the timeout (at most 60 s). No database connection is held while waiting.
- **`semula entry add`** authors a request and its answer up front (text, files, stdin, tool
  calls), the same upsert as MCP; `--version` replaces, `--pin` pins, exit 4 if one exists.
- **Pending screen** in the dashboard (`/ui`): parked calls appear live with a count in the nav;
  select one to read it, answer it in a text box (plain text, or JSON for tool calls and
  streams) and the held call is released. A rejected answer shows the server's error beside the
  field and the call stays held.
- **The CLI as a standalone binary** (about 22 MB, no Python needed) for Linux and macOS, amd64
  and arm64, attached to each GitHub Release with checksums. It has the HTTP commands; `serve`
  and `mcp` stay in the image and the Python package.
- **Mode per project**, kept across restarts (`semula mode record --project <id>`).
- **Redaction** of secrets in everything stored, export and import of a project as JSON files.
- **Observability.** A call log with a live tail, per-minute stats and a prompt preview;
  `test_match` explains each matching tier.
- **A CLI** (`semula`) with `--json` and stable exit codes, and **22 MCP tools** over HTTP and
  stdio, for coding agents. See `docs/agents/PLAYBOOK.md`.
- **A dashboard preview at `/ui`:** the live log. It switches itself off when `SEMULA_API_KEY` is
  set.
- **Databases:** SQLite (a file in the data directory) or Postgres.
- **A container image**, about 244 MB, non-root, starting in about two seconds, with all state on
  one volume.

### Known limits

- Only Chat Completions. Clients that use the OpenAI Responses API or Anthropic Messages cannot
  point at semula yet.
- One shared API key and no accounts; the dashboard is local-only and read-mostly.
- The Codex MCP registration command is unverified.
