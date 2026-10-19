# Agent playbook: recording with semula

You are a coding agent (Claude Code, Codex, or similar). A developer's app is calling an OpenAI-compatible endpoint that is really **semula**. When semula has no recorded answer it **parks** the call and waits for you. Your answer is saved, the app's call completes, and every later identical call replays your answer for free. You are standing in for the paid model, so answer the way it would.

## 1. Connect

Pick one. MCP is preferred; the CLI does the same job.

**Claude Code, MCP over HTTP** (server already running on `127.0.0.1:8080`):

```bash
claude mcp add --transport http semula http://127.0.0.1:8080/mcp/
# if the server has SEMULA_API_KEY set:
claude mcp add --transport http semula http://127.0.0.1:8080/mcp/ --header "Authorization: Bearer $SEMULA_API_KEY"
```

**Any client that only speaks stdio** (`semula mcp` forwards to the running server; set `SEMULA_URL` and, if needed, `SEMULA_API_KEY` in its environment):

```json
{ "mcpServers": { "semula": { "command": "uv", "args": ["run", "--project", "/path/to/semula", "semula", "mcp"], "env": { "SEMULA_URL": "http://127.0.0.1:8080" } } } }
```

**Codex:** register the same stdio command in Codex's MCP configuration (`codex mcp add semula -- uv run --project /path/to/semula semula mcp`). This syntax is not verified against every Codex version; if it differs, use the CLI below.

**CLI only:** `export SEMULA_URL=http://127.0.0.1:8080` (and `SEMULA_API_KEY` if set). Add `--json` to any command for machine-readable output.

## 2. The loop

1. **See what is waiting.** `get_status` shows the mode and counts. Recording needs mode `record` (`set_mode`); in `replay` nothing is parked.
2. **List.** `list_pending` (CLI: `semula pending`). Oldest first. Held calls time out after 300 s by default, but a late answer is still saved.
3. **Read the whole request.** `get_pending <id>` (CLI: `semula show <id>`). Read the system prompt: it says what format the app expects. If `tools` are listed, those are the only tools you may call.
4. **Answer.** `answer_pending <id> <answer>` (CLI: `semula answer <id> --text "..."` or `--file f.json` / `--stdin`).
5. **Verify.** `test_match` with the same request should now report `would: serve`.
6. **Repeat** until `list_pending` is empty.

If your answer is rejected, the call **stays held**. The error names a `code` and a `param`; fix that field and send it again.

## 3. How to answer

| Situation | Send |
|---|---|
| Plain text | `{"content": "..."}` |
| Tool call | `{"message": {"content": null, "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Kuala Lumpur"}}}]}}` |
| A stream with exact chunk boundaries | `{"chunks": ["Hel", "lo ", "world"]}` |
| Several answers (pick by call order) | answer once, then `update_entry` with `add_response` and `selection: "sequence"` or `"cycle"` |

Rules that keep answers replayable:

- **Match the format the app asks for.** If the prompt demands JSON, send valid JSON in `content`. If it demands one word, send one word.
- **Never write a real timestamp, UUID or model name.** Use the templates `{{now}}`, `{{uuid}}`, `{{request.model}}`; they are expanded fresh on every replay. Leave other `{{...}}` alone.
- **Only call tools the request offered**, with arguments that satisfy that tool's JSON schema. Do not invent tools or extra arguments.
- **Be realistic, not creative.** The goal is a plausible response the app can handle, in the shape a real model would give. Avoid markdown the prompt did not ask for.
- **Do not put secrets in answers.** Recorded prompts and answers are stored in plain text.

## 4. Editing what is already recorded

`list_entries` (lean, no bodies) then `get_entry` for the full content. To change one, `update_entry` with the entry's **current `version`**.

| Error `code` | Meaning | What to do |
|---|---|---|
| `version_conflict` (409) | Someone changed it since you read it. The error carries `current`. | Re-read, re-apply your change to that version. Never retry the stale one. |
| `entry_pinned` (409) | A human pinned this entry. | Leave it alone and tell the user. Do not pass `force` unless the user told you to. |
| `entry_exists` (409) | `upsert_entry` found an entry for that request. | Pass its `version` if you mean to replace it. |
| `invalid_answer` (422) | Your answer is malformed. `param` says where. | Fix that field and resubmit. |
| `pending_not_found` (404) | Already answered or deleted. | Move on. |

`upsert_entry` writes an answer **before** any miss happens, from a request body you supply. Use it to pre-record known prompts.

## 5. Matching and settings

`test_match` explains a hit or a miss without side effects: the tier (`exact` or `normalised`), the entry, and on a miss the nearest recorded entry with a score. If near-identical prompts keep missing, adjust the project's settings (`update_project_settings`): `scrub_patterns` (regexes to ignore, e.g. `ticket-\d+`), `tail_n` (match only the last N messages), `include_params` (request params that count). Changing these re-keys existing entries.

## 6. Exit codes (CLI)

`0` ok, `1` unreachable or server error, `2` usage, `3` not found, `4` rejected (invalid, conflict, pinned), `5` authentication failed.
