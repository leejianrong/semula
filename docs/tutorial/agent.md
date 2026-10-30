# Let an agent answer

Typing answers gets old. semula is built so a coding agent (Claude Code or Codex, on the subscription you already have) does it for you. The agent talks to semula over MCP, or through the same CLI you just used.

## Connect the agent

semula serves MCP at `/mcp/` over streamable HTTP. Register that URL once.

=== "Claude Code"

    ```bash
    claude mcp add --transport http semula http://127.0.0.1:8080/mcp/
    claude mcp list
    ```

    ```text
    semula: http://127.0.0.1:8080/mcp/ (HTTP) - ✔ Connected
    ```

=== "Codex"

    ```bash
    codex mcp add semula --url http://127.0.0.1:8080/mcp/
    ```

    For `codex exec` (no one at the keyboard), approve semula's tools in `~/.codex/config.toml`. The sandbox stays on:

    ```toml
    [mcp_servers.semula]
    url = "http://127.0.0.1:8080/mcp/"
    default_tools_approval_mode = "approve"
    ```

If the server has `SEMULA_API_KEY` set, pass the key: `--header "Authorization: Bearer $SEMULA_API_KEY"` for Claude Code, `--bearer-token-env-var SEMULA_API_KEY` for Codex.

## The loop

Put the project in record mode, then give the agent this job:

> You are the recorder for semula. Call `wait_for_pending`, read what it returns with `get_pending`, answer it with `answer_pending` the way the real model would, and repeat.

`wait_for_pending` is the resting state of the loop. It returns the moment a call is parked, at once if some are already waiting, and with an empty list after the timeout (60 seconds at most). The agent never polls.

For a one-shot run, Claude Code needs the tools allowed up front:

```bash
claude -p "You are the recorder for semula. Call wait_for_pending, then get_pending for the first id, then answer_pending with the answer the real model would give. Then say what you answered." \
  --allowedTools "mcp__semula__wait_for_pending,mcp__semula__get_pending,mcp__semula__answer_pending"
```

In a test of this page, an app asked *"Capital of Malaysia?"* with a system prompt demanding one word. The agent waited, read the request, answered `Kuala Lumpur`, and the app's parked call returned it. Codex did the same for a colour question and answered `BLUE`.

!!! tip "Give the agent the playbook"
    The [agent playbook](../agents/PLAYBOOK.md) covers answer formats, error handling and the rules that keep answers replayable. A ready-made Claude Code skill lives in the repository under `docs/agents/claude-code-skill/`.

## Without MCP

The agent can use the CLI instead. Add `--json` for output a program can parse, and `--wait` to block until a call arrives:

```bash
docker exec semula semula pending --wait 60 --json
```

When nothing arrives the output is `[]` and the exit code is `0`.

## What the agent sees when it is wrong

An answer is checked before it is saved. If it is malformed, the call stays held and the error says where:

```text
error: invalid_answer: Unknown tool 'get_forecast'. The request offered: ['get_weather']. (at message.tool_calls[0].function.name)
```

The agent fixes that field and sends it again. The next page shows this with a real tool call.

Next: [tool calls and streaming](tools-and-streaming.md).
