---
name: semula-record
description: Answer parked LLM calls recorded by semula, the record-replay fake LLM server. Use when the user says to record, answer pending requests, fix or pre-record semula entries, or when an app's call is hanging on semula.
---

# Record with semula

semula parks any LLM call it has no recorded answer for. You answer it as the real model would; semula validates and saves it, the app's call completes, and later identical calls replay for free.

Install: copy this folder to `.claude/skills/semula-record/` and connect the MCP server (`claude mcp add --transport http semula http://127.0.0.1:8080/mcp/`, add `--header "Authorization: Bearer $SEMULA_API_KEY"` if the server has a key). With no MCP, use the `semula` CLI with `--json`.

## Loop

1. `get_status`. If the mode is `replay`, nothing is parked; ask before switching to `record` with `set_mode`.
2. `list_pending`, then `get_pending <id>` for the full request. Read the system prompt for the output format, and the tool definitions if any.
3. `answer_pending <id> <answer>`:
   - text: `{"content": "..."}`
   - tool call: `{"message": {"content": null, "tool_calls": [{"function": {"name": "...", "arguments": {...}}}]}}`, only tools the request offered, arguments valid for that tool's schema
   - stream boundaries: `{"chunks": ["Hel", "lo"]}`
4. `test_match` on the same prompt should say `would: serve`. Repeat until nothing is pending.

## Rules

- Match the format the app's prompt demands; if it wants JSON, send valid JSON.
- Never paste a real timestamp, UUID or model name. Use `{{now}}`, `{{uuid}}`, `{{request.model}}`.
- A rejected answer leaves the call held. Read `code` and `param` in the error, fix that field, resubmit.
- On `version_conflict`, re-read the entry and reapply your change to the current version.
- On `entry_pinned`, stop and tell the user. Never pass `force` unless they said to.
- No secrets in answers: everything is stored in plain text.

Full reference: `docs/agents/PLAYBOOK.md`.
