# Errors

Every error from `/api` and `/v1` has the shape OpenAI clients expect, with a stable `code` and, when a field is to blame, a `param`:

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": "version", "code": "entry_exists"}}
```

Some errors carry extra context at the top level: `current` on a 409 holds the entry as it is now, and `nearest` on a replay miss names the closest recorded prompt.

## What your app sees

| Status | `code` | When |
|---|---|---|
| 424 | `semula_no_match` | Replay mode, and nothing recorded matches. `nearest` says what came closest. |
| 504 | `semula_record_timeout` | Record mode, and nobody answered within `SEMULA_RECORD_TIMEOUT` (300 s by default). A late answer is still saved. |
| 429 | `semula_upstream_budget` | The project forwards misses upstream and has used its `upstream_max_calls`. Raise the cap or reset the count (`semula project upstream <id> --reset`); recorded answers still replay. |
| 424 | `semula_upstream_dry_run` | The project is in a dry run: this miss would have been forwarded and nothing was called. |
| 502, 504 | `semula_upstream_error`, `semula_upstream_timeout` | The upstream provider refused, failed or did not answer. The message has its status; the key is never in it. The failed call is not counted against the cap. |
| 401 | `invalid_api_key` | `SEMULA_API_KEY` is set and the request did not send it. |
| 404 | `model_not_found` | `GET /v1/models/<id>` for a model this server does not list. Chat calls ignore the model name. |

## What an agent or the CLI sees

The CLI exits `4` for 400, 409 and 422 and prints `error: <code>: <message> (at <param>)`.

| Status | `code` | Meaning and what to do |
|---|---|---|
| 422 | `invalid_answer` | The answer is malformed. `param` says where. The call **stays held**: fix that field and send it again. |
| 409 | `version_conflict` | Someone changed the entry since you read it. `current` holds the new one. Re-read and re-apply; never retry the stale write. |
| 409 | `entry_pinned` | A human pinned this entry. Leave it alone and tell the user. Pass `force` only if told to. |
| 409 | `entry_exists` | `entry add` or `upsert_entry` found an entry for that request. Pass its `version` to replace it. |
| 409 | `already_answered` | The parked request was answered first by someone else. Move on. |
| 404 | `pending_not_found` | No such parked request. It was answered or deleted. |
| 404 | `entry_not_found`, `project_not_found`, `candidate_not_found` | The id does not exist. |
| 409 | `candidate_decided` | The review candidate was already approved or rejected. |
| 409 | `project_exists`, `default_project` | A project with that id exists, or you tried to delete `default`. |
| 409 | `entry_in_other_project`, `alias_in_other_project` | An id belongs to a different project. |
| 400 | `version_required` | An edit needs the entry's current `version`. |
| 400 | `invalid_json`, `invalid_request`, `nothing_to_update` | The request body is not what the route expects. |
| 422 | `invalid_mode`, `invalid_selection`, `invalid_pinned`, `invalid_settings`, `invalid_project_id`, `invalid_param`, `invalid_bundle` | A value is out of range or malformed. `param` names it. |
| 422 | `upstream_not_configured` | `record_through` was turned on but the server has no `SEMULA_UPSTREAM_KEY`. |
| 503 | `mcp_unavailable` | The MCP endpoint could not start. |
| 500 | `internal_error` | A bug. Please report it with the log line. |
