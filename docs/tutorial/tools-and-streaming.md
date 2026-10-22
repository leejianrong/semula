# Tool calls and streaming

Real apps ask for tool calls and stream tokens. semula records both.

## A tool call

Save this request as `weather.json`. It offers one tool:

```json
{
  "model": "gpt-4o",
  "messages": [{"role": "user", "content": "What is the weather in Ipoh?"}],
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_weather",
      "description": "Current weather for a city",
      "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"]
      }
    }
  }]
}
```

Send it with the project in record mode. It parks:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' -d @weather.json
```

Answer with a tool call. `arguments` can be an object; semula stores it as the JSON string OpenAI clients expect:

```json
{"message": {"content": null, "tool_calls": [{"function": {"name": "get_forecast", "arguments": {"city": "Ipoh"}}}]}}
```

That answer calls `get_forecast`, a tool the request never offered. semula says so and keeps the call held:

```
error: invalid_answer: Unknown tool 'get_forecast'. The request offered: ['get_weather']. (at message.tool_calls[0].function.name)
```

The exit code is `4`. Fix the name, or reply in plain text instead:

```bash
docker exec semula semula answer <id> --text "I cannot check the weather."
```

Arguments are checked against the tool's JSON schema too, so a missing `city` is rejected the same way.

!!! note "Putting JSON in a file"
    `--file answer.json` or `--stdin` take any answer as JSON. `--text` is only for plain text.

## Streaming

A streaming request records the same way. Ask for `"stream": true`:

```bash
curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "gpt-4o", "stream": true, "messages": [{"role": "user", "content": "Say hello slowly."}]}'
```

Answer with the exact chunk boundaries you want your app to see:

```bash
echo '{"chunks": ["Hel", "lo ", "there"]}' | docker exec -i semula semula answer <id> --stdin
```

On replay the app receives one server-sent event per chunk, then `[DONE]`:

```
data: {"id":"chatcmpl-34df…","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-34df…","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}

data: {"id":"chatcmpl-34df…","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"content":"lo "},"finish_reason":null}]}

data: {"id":"chatcmpl-34df…","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"content":"there"},"finish_reason":null}]}

data: {"id":"chatcmpl-34df…","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

A plain text answer to a streaming request is split into chunks for you. Either kind of recording serves both streaming and non-streaming requests.

Next: [one project per app](projects.md).
