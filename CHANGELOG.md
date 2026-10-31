# Changelog

All notable changes to semula are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). semula is 0.x: a minor release may change
behaviour, and each such change is listed under "Changed". Recordings made with one release
open in the next; the database upgrades itself on start and cannot be downgraded.

## [Unreleased]

### Added

- `deploy/droplet`: the hosted server on one machine as a compose project: Caddy with automatic TLS and HSTS, semula on the hosted image, Postgres with a separate owner and app role (so row-level security applies), a migrate step before the app, a daily workspace purge and an `admin` service for operator commands. Only ports 80 and 443 are published. `make droplet-smoke` runs the whole stack and checks it.
- `SEMULA_TRUSTED_PROXIES`: behind a reverse proxy, the failed-token limit, a token's last-used network and the audit log use the real client from `X-Forwarded-For` instead of the proxy's address, believed only from the proxies you name.

### Changed

- A hosted server no longer prints the "no SEMULA_API_KEY" warning when it listens beyond loopback: tokens protect it.
- `semula serve` no longer lets uvicorn rewrite the client address from `X-Forwarded-For` for a peer on loopback. Set `SEMULA_TRUSTED_PROXIES` if a proxy on the same host should be believed.

## [0.4.0] - 2026-10-27

This release is for answering at scale: a ceiling on runaway misses, a way to read what an answer
must look like, several workers sharing one queue, long games read as conversations, and an
answerer that runs your command once per parked call. Everything new is off by default or only
adds fields, so nothing changes for anyone who does not use it.

**Before you upgrade, back up your data** (see [Upgrade and back up](docs/guide/upgrade-and-backup.md)).
Migration 0019 runs on start; it adds two nullable columns to `pending` and cannot be undone by
installing 0.3.0 again.

### Changed

- A parked call in `GET /api/v1/pending` gains `claimed_by`, `claimed_until` and `conversation`;
  `GET /api/v1/pending/{id}` (and `semula show`, MCP `get_pending`) gains `shape` and a fuller
  `conversation`. Nothing was removed. A project's settings gain `max_misses`, `misses_since` and
  `answerer`; none is exported.

### Added

- The answerer hook (ADR-0014): set `SEMULA_ANSWERER_COMMAND` (a JSON argv, for example `claude -p ...`) or `SEMULA_ANSWERER_URL`, then turn on the project setting `answerer`. Each parked call is answered by one fresh process (or one webhook POST) with the request on stdin as JSON, in an empty directory with a stripped environment, a timeout, an output cap and a concurrency cap. A rejected answer is sent back once with the reason; a failure or timeout leaves the call parked. Off by default, admin-only and audited, and refused on a hosted server. `semula claim` skips a call the hook holds. No migration.
- Group calls by conversation (`semula conversations`, `GET /api/v1/conversations`, MCP `list_conversations`, a Conversation column and an "added since the previous call" row on the dashboard's pending page). A call whose messages extend an earlier call's joins its group and shows the messages it added; `semula show` and `get_pending` carry the same under `conversation`. Prefix only, no semantic grouping. No migration.
- `claim_next` (`semula claim`, `POST /api/v1/pending/claim`, MCP `claim_next`): take the oldest unclaimed parked call, in full, held for a lease so several workers never answer the same one. An expired lease frees the call. The pending list shows `claimed_by` and `claimed_until`. Migration 0019 adds two nullable columns to `pending`; it runs on start and cannot be undone by installing an older version.
- A parked call now shows the shape of the answer it allows (`shape` on `GET /api/v1/pending/{id}`, `semula show`, the MCP `get_pending` and the dashboard): each offered tool with its argument schema and a skeleton answer that passes validation, and the `max_tokens` limit when the app set one.
- `max_misses`, a project setting: the most distinct misses (parked or answered) a project takes in a run. After that a miss fails with 429 `semula_miss_ceiling` and is not parked, so a loop in the app cannot park hundreds of calls. Replay hits are never counted. Setting it to a new value starts a new run. No migration.

### Fixed

- Ids made in the same millisecond could sort out of the order they were made, so "oldest first" (the pending list, `claim_next`, conversation grouping) could be wrong for calls that close together. Ids from one process now strictly increase.

## [0.3.0] - 2026-10-25

This release adds record-through: a project can capture a real model's answers once and replay
them free. It is off by default and changes nothing for anyone who does not turn it on.

**Before you upgrade, back up your data** (see [Upgrade and back up](docs/guide/upgrade-and-backup.md)).
Migration 0018 runs on start; it adds one table and cannot be undone by installing 0.2.0 again.

### Changed

- `GET /api/v1/log/stats` (and `semula log --stats`, the MCP `log_stats` tool) gains `forwarded` in
  its totals and in each bucket, and an `upstream` object with the calls and tokens forwarded so
  far. Nothing was removed. A project's settings gain `record_through`, `upstream_model`,
  `upstream_max_calls`, `upstream_max_tokens` and `upstream_dry_run`; none is exported.

### Added

- `.env.example`: the variables for a record-through trial against OpenRouter (or any
  OpenAI-compatible provider), to copy to the git-ignored `.env` and start with
  `uv run --env-file .env semula serve`.
- **Record-through** (SEM-92, [ADR-0013](docs/adr/0013-record-through-and-its-budget.md)): a project
  can forward a miss to a real OpenAI-compatible provider, save the answer (chunks and usage when
  streamed) and replay it free afterwards. Off by default and per project: set `record_through`
  with the project in record mode and `SEMULA_UPSTREAM_KEY` on the server. The key lives only in
  the server's environment and is never stored, logged, exported or echoed in an error. Each
  project has a hard cap, `upstream_max_calls` (default 100); past it a miss fails with
  `semula_upstream_budget`. Replay mode never forwards. Migration 0018 adds the counter table.
- **A budget and a dry run for record-through** (SEM-93): `upstream_max_tokens` caps tokens too,
  `upstream_dry_run` forwards nothing and lists the prompts that would have been called, and
  `semula project upstream <id>` (REST, MCP `get_upstream_usage`) shows calls and tokens against the
  caps, with `--reset` to start again. Stats gain `forwarded` and `upstream`; the dashboard log page
  shows both.
- **A hosted image**, `ghcr.io/leejianrong/semula:<version>-hosted` (also `<major>.<minor>-hosted`
  and `latest-hosted`), published beside the default one from the next release on. It adds the
  sign-in (PyJWT) and Postgres (psycopg) drivers and is about 22 MB larger; the release smoke-tests
  it by minting a token in it and calling with and without that token. Hosting 0.2.0 still needs
  your own build (`--build-arg UV_EXTRAS="--extra hosted --extra postgres"`).
- **A workspace switcher** in the hosted dashboard for people who belong to more than one
  workspace. The choice is kept by the sign-in; the open tab ends and a reload opens the new
  workspace (the dashboard framework cannot reload a page from the server).

## [0.2.0] - 2026-10-23

This release adds hosted semula (one server for several teams, with sign-in, roles and API
tokens) and the rest of the dashboard. Local use is unchanged.

**Before you upgrade, back up your data** (see [Upgrade and back up](docs/guide/upgrade-and-backup.md)).
Migrations 0010 to 0017 run on start. On SQLite, 0010 rebuilds six tables, so it takes longer on a
large file and cannot be undone by installing 0.1.0 again.

**Hosted mode needs more than the default image.** Signing in uses PyJWT (the `hosted` extra),
and hosting wants Postgres (the `postgres` extra); the published image has neither, to stay
small. Build your own with
`docker build --build-arg UV_EXTRAS="--extra hosted --extra postgres" .`
(see [Host semula for a team](docs/guide/hosted.md)). The published image and the CLI binaries
are for local use.

### Changed

- The always-on laptop service (`deploy/laptop`) listens on port 8731 by default instead of 8787.
  Set `SEMULA_PORT` to keep the old one.

### Added

- **The hosted dashboard** (ADR-0010): with a sign-in provider set up, `/ui` is behind sign-in.
  A page load without one goes to the sign-in page and the app's own calls get a 401; a session
  belongs to one person and one workspace and sees only its data. Viewers, editors and admins are
  given different controls (a control a role may not use is not sent to the browser) and the role
  is read again on every action, so a demotion or removal takes effect at the next click. Admins
  get Members (roles, remove, invite: the link is shown once), Tokens (create, rotate, revoke:
  the secret is shown once), Audit (filter, paging) and Usage, with deleting the workspace by
  typing its id back. A strict content security policy and security headers are on.
- **Members, roles and invitations** (migration 0017, ADR-0010): viewer, editor and admin, per
  workspace; the last admin cannot leave or be demoted; invitations by email, single use, seven
  days, accepted only by a signed-in person whose verified email matches; `/auth/invite/<token>`;
  `semula admin invite` and `members`. A role is enforced in the service on every method a person
  reaches (a decorator, and a test fails for a method without one).
- **People can sign in** (migration 0016, ADR-0010): OpenID Connect (Google, or any issuer) and
  GitHub, with PKCE, `state`, `nonce`, issuer and audience checks, signature and expiry
  verification and verified-email-only; a person is (issuer, subject), never an email. Opaque
  server-side sessions (12 h idle, 7 days), a new one at each sign-in, sign-out with a CSRF token.
  Install the `hosted` extra for PyJWT.
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
