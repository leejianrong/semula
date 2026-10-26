# MCP tools

semula serves MCP at `/mcp/` over streamable HTTP, and `semula mcp` is a stdio proxy to it for clients that cannot speak HTTP. Every write over MCP is made as an *agent*: a pinned entry is protected unless the tool is given `force`.

Arguments marked `?` are optional. The tool descriptions the agent sees are longer than the summaries here, and they are the product: they steer an agent toward answers that replay cleanly. The [agent playbook](../agents/PLAYBOOK.md) is the same guidance in prose.

| Tool | Arguments | What it does |
|---|---|---|
| `get_status` |  | The server's mode and version, and each project's mode, entry count and pending count. Start here. |
| `set_mode` | `mode` `project`? | Switch a project between `record` and `replay`. Pass `project` so the choice is kept. |
| `list_projects` |  | Projects with their entry and pending counts. |
| `create_project` | `id` `name`? | Create a project (a slug: lowercase letters, digits, hyphens). |
| `get_project_settings` | `id` | A project's matching and recording settings, with defaults. |
| `get_upstream_usage` | `project` | Calls and tokens a project has forwarded upstream, its caps, and the prompts a dry run would forward. |
| `update_project_settings` | `id` `settings` | Patch settings; `null` resets one. Changing a matching setting re-keys existing entries. |
| `list_pending` | `project`? `include_answered`? | Parked requests waiting for an answer, oldest first. |
| `wait_for_pending` | `project`? `timeout_seconds`? | Block until a call is parked (or return what already waits); empty at the timeout, 60 s at most. |
| `get_pending` | `id` | One parked request in full: messages, offered tools, and a `shape` with each tool's argument schema, a valid skeleton answer and the free-text limit. |
| `claim_next` | `project`? `lease_seconds`? `worker`? | The oldest parked request nobody holds, in full, held for you for the lease (default 300 s, at most 3600); `null` when none. Other claims skip it until the lease ends. |
| `answer_pending` | `id` `answer` | Answer a parked request. This releases the app's held call. A rejected answer leaves it held. |
| `list_entries` | `project` `q`? `limit`? `offset`? `pinned`? `has_alias`? `tool_calls`? `never_hit`? `sort`? `order`? | A project's entries without bodies, filtered and sorted. |
| `get_entry` | `id` | One entry in full, including its `version`. |
| `entry_history` | `id` `version`? | An entry's change history, newest first; `version` returns that revision in full. |
| `upsert_entry` | `project` `request` `response` `version`? | Author an entry before any miss. Pass `version` to replace an existing one. |
| `update_entry` | `id` `version` `changes` `force`? | Change an entry at its current `version` (response, selection, pin). |
| `delete_entry` | `id` `version` `force`? | Delete an entry at its current `version`. Pinned entries are refused unless forced. |
| `test_match` | `project`? `prompt`? `request`? | What would happen to a request now: the tier, the entry, and why a miss missed. No side effects. |
| `list_calls` | `project`? `outcome`? `limit`? | Recent calls, newest first, with a preview of the prompt. |
| `log_stats` | `project`? `window`? | Per-minute counts (including forwarded), hit rate, median latency and how many calls are parked. |
| `list_candidates` | `project`? `include_decided`? | The review queue: near misses waiting for a decision. |
| `get_candidate` | `id` | A candidate next to the entry it resembles. |
| `approve_candidate` | `id` | Serve this prompt from that entry from now on. |
| `reject_candidate` | `id` | Never pair these two prompts again. |

## Errors

A failed tool call returns JSON the agent can act on:

```json
{"error": {"code": "invalid_answer", "message": "Unknown tool 'get_forecast'. ...", "param": "message.tool_calls[0].function.name"}}
```

See [Errors](errors.md) for the codes.
