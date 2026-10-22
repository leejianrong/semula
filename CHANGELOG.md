# Changelog

All notable changes to semula are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). semula is 0.x: a minor release may change
behaviour, and each such change is listed under "Changed". Recordings made with one release
open in the next; the database upgrades itself on start and cannot be downgraded.

## [Unreleased]

### Added

- **Replicas** (ADR-0011): on Postgres, replicas share live events and wake held calls over
  `LISTEN/NOTIFY`; publishing never waits for the network and the waits look again each second.
  `SEMULA_MIGRATE=off` with `semula admin migrate` migrates once before rollout; starts that do
  migrate together take turns.
- **Deleting a workspace** (migration 0015, ADR-0011): marked deleted with the id typed back,
  its tokens stop at once, restorable for 30 days, then purged in dependency order; the audit log
  keeps the fact. `semula admin workspace-delete`, `-restore` and `-purge`.
- **Quotas and limits** for a hosted workspace (migration 0014, ADR-0011): projects, entries,
  stored bytes, parked calls, tokens, request body size, `record_timeout`, and a per-token rate
  limit, each with a clear error naming the limit. Counters are kept in the write's own
  transaction, so replay hits never pay for them. `semula usage`, `GET /api/v1/usage` and
  `semula admin recount`. A local server has no limits.
- **Audit log** (migration 0013, ADR-0010): an append-only record of tokens, projects (create,
  rename, delete, settings, mode, import), entry deletes, forced writes and deletes, and review
  decisions, with the principal, request id and address. It stores ids and counts, never content;
  a trigger refuses edits and early deletes. `semula audit` and `GET /api/v1/audit` read it.
- **Hosted mode and API tokens** (`SEMULA_HOSTED=on`, ADR-0010): every call needs an opaque,
  hashed `smla_live_` token that names its workspace, one of three scopes (`replay`, `record`,
  `admin`) and optionally one project. A replay token never parks a call; a record token cannot
  delete, approve, force past a pin or change settings; the actor comes from the token, not a
  header; failed attempts are rate limited; revoking is immediate in-process and within 30 s
  elsewhere. `semula admin` creates workspaces and mints, lists, rotates and revokes tokens.
  Tokens in prompts are redacted. A project-bound token reaches its project and nothing else,
  through REST and through MCP (each tool has a scope; browser `Origin`s are refused). The open
  dashboard stays off in hosted mode.
- **Postgres row-level security** (migration 0011): tenant tables carry a policy on
  `workspace_id`, and every transaction states which workspace it acts for, so a query that forgot
  its filter sees nothing of another workspace's. Owners and superusers bypass it; see the
  configuration reference.
- **Composite keys** (migration 0010): an entry, pending request, review candidate or alias is
  keyed by its workspace and id, and its children point at their entry inside the same workspace.
  The same id can now live in two workspaces, so importing one bundle into two workspaces works
  and a row cannot name another workspace's entry. The migration keeps every row; SQLite rebuilds
  six tables, so back up first, as with any upgrade.
- **Workspace isolation in the service** (groundwork for hosting, ADR-0011): the service is bound
  to one workspace per request, every query is filtered by it, events and the log stream carry
  their workspace, and a test calls every method as another workspace to prove nothing leaks.
  It found one leak, fixed: a waiter could read another workspace's answer by pending id. Local
  use has one workspace and sees no change.
- **Playground**: type a prompt (with an optional system prompt and tools) and see what semula
  would do with it in this project: the tier that answers and its score, the recorded answer, or
  the nearest prompt on a miss, and whether the call would park or fail. Nothing is served,
  logged or counted.
- **Review screen**: the near misses of a project with their score and tier, a side-by-side
  comparison of the new prompt against the recorded one (changed words marked), the recorded
  answer with a reminder to check it fits, and Approve and Reject. The nav shows how many wait.
- **New entry** on the Entries screen: write a prompt (with an optional system prompt and
  tools) and its answer, optionally pin it, and see the reason beside the field when it is
  rejected. An existing prompt is never overwritten from the form.
- **Entries screen** in the dashboard: every recorded entry of a project with search, a filter
  menu (pinned, never hit, tool calls, aliases), sort, hit counts and flags, kept up to date as
  entries are recorded. Selecting one opens it at its own address with its request, answers and
  history. The entry page is also an editor: change the answer (plain text, or JSON for a tool call or
  a stream), choose how several answers are picked, add another answer, pin or unpin, delete.
  Edits are made as a person, so a pin does not stop them, and a stale edit meets a version
  conflict instead of overwriting someone else's change. The service now announces entry writes (`entries` events) for live views.

## [0.1.0] - 2026-10-22

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
- **Documentation site** (Zensical, on GitHub Pages): a tutorial of nine short pages, an install
  and operations guide, and reference tables for the CLI, MCP tools, REST API, settings and
  errors that tests keep equal to the code. `make docs` builds it, `make docs-serve` previews it.
- **A clear refusal to downgrade.** Starting semula on a database that a newer release migrated
  now says so and exits, instead of printing an Alembic traceback.
- **Mode per project**, kept across restarts (`semula mode record --project <id>`).
- **Redaction** of secrets in everything stored, export and import of a project as JSON files.
- **Observability.** A call log with a live tail, per-minute stats and a prompt preview;
  `test_match` explains each matching tier.
- **A CLI** (`semula`) with `--json` and stable exit codes, and **23 MCP tools** over HTTP and
  stdio, for coding agents (checked with Claude Code and Codex). See `docs/agents/PLAYBOOK.md`.
- **A dashboard at `/ui`:** the live log and the Pending screen. It switches itself off when
  `SEMULA_API_KEY` is set.
- **Databases:** SQLite (a file in the data directory) or Postgres.
- **A container image** at `ghcr.io/leejianrong/semula` (amd64 and arm64), about 244 MB, non-root, starting in about two seconds, with all state on
  one volume.

### Known limits

- Only Chat Completions. Clients that use the OpenAI Responses API or Anthropic Messages cannot
  point at semula yet.
- One shared API key and no accounts; the dashboard is local-only. It has the live log and the
  Pending screen; browsing and editing entries, the review queue and a playground are in the
  CLI, MCP and REST for now.
- The standalone CLI binary has the HTTP commands only (no `serve`, `mcp` or `embeddings fetch`).
