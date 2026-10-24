# Configuration

semula reads its settings from the environment. Every one has a default that works on a laptop.

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `SEMULA_MODE` | `replay` | The server default mode, `replay` or `record`. A project that has chosen its own mode ignores it. Forgotten on restart. |
| `SEMULA_HOST` | `127.0.0.1` | The address to listen on. The image sets `0.0.0.0`. |
| `SEMULA_PORT` | `8080` | The port to listen on. |
| `SEMULA_DATA_DIR` | `./data` | Where the SQLite file and the embedding model live. The image uses `/data`. |
| `SEMULA_DATABASE_URL` | a SQLite file in the data directory | A `postgresql://` URL to use Postgres instead. |
| `SEMULA_RECORD_TIMEOUT` | `300` | Seconds a parked call waits for an answer. A project can override it. |
| `SEMULA_CHUNK_SIZE` | `24` | Characters per chunk when a plain-text answer is replayed as a stream. |
| `SEMULA_MODELS` | `semula` | Comma-separated model ids that `/v1/models` lists. |
| `SEMULA_QUOTA_<NAME>` | hosted defaults | Override a [hosted limit](../guide/hosted.md#limits), for example `SEMULA_QUOTA_PROJECTS=50`; `none` removes it. Names: `PROJECTS`, `ENTRIES`, `BYTES`, `PARKED_PER_PROJECT`, `PARKED_PER_WORKSPACE`, `TOKENS`, `MEMBERS`, `BODY_BYTES`, `RECORD_TIMEOUT_MAX`, `RATE`, `BURST`. A local server has none. |
| `SEMULA_MIGRATE` | `on` | `off` stops a start from migrating the database and refuses to start if it is not up to date. For replicas: run `semula admin migrate` once before rollout. |
| `SEMULA_UI` | `on` | `off` hides the dashboard at `/ui`. |
| `SEMULA_API_KEY` | none | Require this key. See below. |
| `SEMULA_PUBLIC_URL` | `http://127.0.0.1:8080` | The address people reach the server at, no trailing slash. Sign-in redirect URIs and cookie names come from it; use `https://...` when hosted. |
| `SEMULA_SECRET` | none | At least 16 random bytes, to sign sign-in state. Needed once any sign-in provider is set up. |
| `SEMULA_AUTH_PROVIDERS` | none | Comma-separated sign-in providers, such as `google,github`. For each name `N` set `SEMULA_AUTH_N_CLIENT_ID` and `SEMULA_AUTH_N_CLIENT_SECRET`; any name other than `google` and `github` is a generic OpenID Connect issuer and also needs `SEMULA_AUTH_N_ISSUER`. |
| `SEMULA_HOSTED` | `off` | `on` makes every call need an [API token](../guide/hosted.md) that names its workspace, scope and project. The dashboard is available only behind a sign-in provider (see [the dashboard](../guide/hosted.md#the-dashboard)). |
| `SEMULA_LOG_RETENTION_DAYS` | `30` | How long the call log is kept. |
| `SEMULA_LOG_MAX_ROWS` | `50000` | The most call-log rows kept. |
| `SEMULA_EMBEDDINGS` | `off` | `on` turns on meaning-based matching. Needs the `embeddings` extra. |
| `SEMULA_EMBEDDINGS_OFFLINE` | `off` | `on` forbids downloading the model; start fails if it is not cached. |
| `SEMULA_MODEL_DIR` | `<data dir>/models` | Where the embedding model is stored. |
| `SEMULA_UPSTREAM_KEY` | none | The API key of a real provider that projects with `record_through` forward misses to. Never stored, logged or exported. See [Record through a real model](../guide/service.md#record-through-a-real-model). |
| `SEMULA_UPSTREAM_URL` | `https://api.openai.com/v1` | The provider's OpenAI-compatible base URL, no trailing slash. |
| `SEMULA_UPSTREAM_TIMEOUT` | `120` | Seconds to wait for the provider. |
| `SEMULA_UPSTREAM_TENANTS` | `off` | Hosted only: `on` lets a workspace admin turn `record_through` on with the operator's key. |
| `SEMULA_URL` | `http://127.0.0.1:8080` | For the CLI: which server to talk to. |

## Protecting a server

By default semula trusts whoever can reach its port. That is fine on loopback. Before you publish the port beyond your machine, set a key:

```bash
docker run -d -p 8080:8080 -e SEMULA_API_KEY=change-me ghcr.io/leejianrong/semula
```

Then `/v1`, `/api` and `/mcp` need `Authorization: Bearer <key>` (or `X-API-Key`). `/health` stays open. The CLI reads the key from `SEMULA_API_KEY`.

!!! warning "The dashboard turns itself off"
    A browser cannot send the key on a page load, so with a key set the dashboard is off. Sign-in for the dashboard is part of the hosted design (ADR-0010).

`semula serve` also warns when it listens on a non-loopback address with no key.

## Project settings

Matching and recording are tuned per project, not globally. Show them with `semula project settings <id>` and change them with `--set '{...}'`; `null` resets one.

| Setting | Default | What it does |
|---|---|---|
| `models` | none | Model ids this project lists. |
| `tail_n` | none | Match only the last N messages. |
| `scrub_patterns` | none | Regexes to ignore when matching, for example `ticket-\d+`. |
| `include_params` | none | Request parameters that count, for example `["temperature"]`. |
| `record_timeout` | server value | Seconds a parked call waits. |
| `redact_patterns` | none | Regexes for secrets: stored as `[REDACTED]` and ignored when matching. |
| `redact_defaults` | `true` | Also redact common API keys and tokens. |
| `bm25_serve_threshold` | `1` | Word-overlap score (0 to 1) at which a close match is served. |
| `bm25_review_threshold` | `0.6` | The floor for the review queue. |
| `embedding_review_threshold` | `0.75` | Cosine score at which a paraphrase goes to review. |
| `embedding_serve_threshold` | none | Serve paraphrases at this score. Unset means never. |
| `record_through` | `false` | Forward a miss to the server's upstream provider and save its answer. Needs the project in record mode and `SEMULA_UPSTREAM_KEY`. Never exported. |
| `upstream_model` | none | Send this model upstream instead of the one the request names. Never exported. |
| `upstream_max_calls` | `100` | Most calls the project may forward; a miss after that fails with `semula_upstream_budget`. Never exported. |
| `upstream_max_tokens` | none | Most prompt plus completion tokens the project may spend upstream. Known only after a call, so one call can overshoot. Never exported. |
| `upstream_dry_run` | `false` | With `record_through` on, forward nothing: a miss fails with `semula_upstream_dry_run` and is listed by `semula project upstream`. Never exported. |

Changing a matching setting (`tail_n`, `scrub_patterns`, `include_params`) re-keys the project's existing entries. Nothing has to be re-recorded.

## Postgres

SQLite needs nothing. For a shared setup, install the extra and point semula at Postgres. Migrations run on start for both:

```bash
uv sync --extra postgres
SEMULA_DATABASE_URL=postgresql://user:pass@host:5432/semula uv run semula serve
```

In the image, build with `--build-arg UV_EXTRAS="--extra postgres"`.

!!! note "Row-level security"
    On Postgres every tenant table has a row-level-security policy that compares its `workspace_id` with the workspace the connection is acting for, as a second line of defence behind the application's own filters. The role that owns the tables and any superuser bypass policies, which is what you want for migrations and a single-user setup. When several workspaces share a database, run semula as a role that is neither, and migrate with the owner first.

## Semantic matching

Paraphrases share few words, so the word tiers miss them. With the `embeddings` extra installed and `SEMULA_EMBEDDINGS=on`, semula also compares meaning. The first start downloads a 65 MB model into the model directory; later starts need no network. `semula embeddings fetch` downloads it ahead of time, and `semula embeddings status` shows progress. Paraphrases go to the review queue with their score and are never served unless you set `embedding_serve_threshold`.
