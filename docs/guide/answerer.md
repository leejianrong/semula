# How to let a command answer each call

This guide shows you how to answer parked calls with a command you choose, with no agent in the loop. Use it for unattended runs and evals.

An agent answering many calls in one context sees the earlier prompts, which spoils an eval, and it cannot run unattended. The answerer hook starts one fresh process per parked call instead. The command is yours, set in the server's environment, and the request goes to it on stdin as JSON:

```bash
export SEMULA_ANSWERER_COMMAND='["claude","-p","You are the model the app calls. Answer as it would, in the format its prompt asks for. For a request with tools, reply only with JSON as in shape."]'
semula serve &
semula project settings game --set '{"mode": "record", "answerer": true, "max_misses": 200}'
```

What it does, call by call:

- Each call gets its own process in its own empty temporary directory, with `HOME`, `PATH` and a few basics in its environment and nothing else. It never gets a `SEMULA_` variable, so your upstream key and API key stay out of reach. Name more with `SEMULA_ANSWERER_ENV`, for example `ANTHROPIC_API_KEY`.
- stdin is `{"request": {messages, tools, params}, "shape": {...}, "project", "pending_id", "attempt"}`. `shape` is the same view `semula show` gives: each tool's schema and a skeleton call. stdout is the answer: JSON that `semula answer` would take, or plain text for a plain reply (one code fence around the JSON is removed).
- An answer is checked like any other. If it is rejected, the command is run once more with `rejection` and `previous_output` added; a second rejection leaves the call parked.
- A failure, a timeout (`SEMULA_ANSWERER_TIMEOUT`, 120 s; the whole process group is killed), no output, or more than 1 MiB of output leaves the call parked for an agent or a person to answer. Nothing is retried while the server runs, so a broken command cannot loop; restarting tries again.
- While it works the hook holds the call, so `semula claim` skips it. At most `SEMULA_ANSWERER_CONCURRENCY` (2) run at once.
- `SEMULA_ANSWERER_URL` does the same with a webhook: one POST of that JSON, the answer in the response body, redirects not followed. Add `SEMULA_ANSWERER_BEARER` if it needs a token.

Before you turn it on, note these risks:

- **Set `max_misses`.** An app that parks hundreds of calls starts hundreds of processes, and with `claude -p` each is a model call.
- **A parked prompt is untrusted text** and may try to instruct your answerer. semula never runs what the command returns (it is data, validated and stored), but the command itself reads the prompt, so what it can do is what you let it do. Run the model with tools off; an answerer needs none. semula gives the child an empty directory and a stripped environment, not a sandbox.
- **The command is the operator's.** It is never a project setting, never read from a request, and turning the switch on needs the admin role and is audited. A hosted server refuses it.
- Needs a POSIX system (it uses process groups). The project's tests use a stand-in script; none runs a real model, so check your own `claude -p` setup on one call first.

## Related

- [Configuration](../reference/configuration.md) lists every `SEMULA_ANSWERER_*` variable.
- [Manage a busy queue](busy-queue.md) for `max_misses`, which you should set first.
