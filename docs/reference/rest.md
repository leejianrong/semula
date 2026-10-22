# REST API

Two APIs share the port. `/v1` is the OpenAI-compatible surface your app calls. `/api/v1` is the admin surface that the CLI, the MCP server and the dashboard all use, and you can call it too. Both honour `SEMULA_API_KEY` when it is set. Errors are described in [Errors](errors.md).

## For your app: `/v1`

| Method and path | What it does |
|---|---|
| `POST /v1/chat/completions` | Chat completions, streaming and non-streaming, with tool calls. Pick a project with the `X-Semula-Project` header. |
| `POST /v1/projects/{project_id}/chat/completions` | The same, with the project in the path. Use this as the SDK's `base_url`. |
| `GET /v1/models` | The model ids semula lists (`SEMULA_MODELS`). |
| `GET /v1/models/{model_id}` | One model, or `404 model_not_found`. |
| `GET /v1/projects/{project_id}/models` | The models for one project. |
| `GET /v1/projects/{project_id}/models/{model_id}` | One model for one project. |
| `GET /health` | `{"status": "ok", "version": ..., "mode": ...}`. Always open. |

## For people and agents: `/api/v1`

| Method and path | What it does |
|---|---|
| `GET /api/v1/projects` | Projects with entry and pending counts. |
| `POST /api/v1/projects` | Create one: `{"id": "my-app", "name": "My app"}`. |
| `GET /api/v1/projects/{project_id}` | One project with its settings and defaults. |
| `PATCH /api/v1/projects/{project_id}` | Rename it or patch `settings`. |
| `DELETE /api/v1/projects/{project_id}` | Delete it and everything in it. |
| `GET /api/v1/projects/{project_id}/entries` | List entries without bodies. Filters: `q`, `pinned`, `has_alias`, `tool_calls`, `never_hit`; `sort`, `order`, `limit`, `offset`. |
| `POST /api/v1/projects/{project_id}/entries` | Author an entry: `{"request": {...}, "response": ..., "version"?: n}`. `201` if created, `200` if replaced. |
| `GET /api/v1/entries/{entry_id}` | One entry in full. |
| `PUT /api/v1/entries/{entry_id}` | Change it. Needs `version`; fields `response`, `add_response`, `selection`, `pinned`, `force`. |
| `DELETE /api/v1/entries/{entry_id}` | Delete it. Needs `?version=`. |
| `GET /api/v1/entries/{entry_id}/history` | Revisions, newest first. `?version=N` for one in full. |
| `POST /api/v1/projects/{project_id}/test-match` | What would happen to a request, tier by tier. |
| `GET /api/v1/mode` | The server default and each project's mode. |
| `PUT /api/v1/mode` | Set the mode: `{"mode": "record", "project"?: id}`. |
| `GET /api/v1/pending` | Parked calls. `?project=`, `?status=all`, and `?wait=SECONDS` (up to 60) to hold until one is parked. |
| `GET /api/v1/pending/{pending_id}` | One parked call in full. |
| `POST /api/v1/pending/{pending_id}/answer` | Answer it. The body is the answer. Releases the held call. |
| `GET /api/v1/candidates` | The review queue. |
| `GET /api/v1/candidates/{candidate_id}` | One candidate next to its entry. |
| `POST /api/v1/candidates/{candidate_id}/approve` | Serve that prompt from the entry from now on. |
| `POST /api/v1/candidates/{candidate_id}/reject` | Never pair them again. |
| `GET /api/v1/log` | Recent calls, newest first. |
| `GET /api/v1/log/stats` | Counts, hit rate, median latency. `?window=30m`. |
| `GET /api/v1/log/stream` | The same feed as server-sent events. |
| `GET /api/v1/projects/{project_id}/export` | A `semula/1` bundle of a project. |
| `POST /api/v1/import` | Load a bundle: `{"bundle": {...}, "project"?: id, "force"?: bool}`. Idempotent. |
| `GET /api/v1/embeddings` | Whether semantic matching is on and how far indexing has got. |

### Who is writing

A write is treated as an *agent* unless the request sends `X-Semula-Actor: human`. Agents cannot change a pinned entry. The CLI's `--as-human` sends that header.

### Wait for a parked call

```bash
curl "http://127.0.0.1:8080/api/v1/pending?wait=30"
```

Returns as soon as a call is parked, at once if some already wait, and `{"pending": []}` after 30 seconds. No database connection is held while it waits.
