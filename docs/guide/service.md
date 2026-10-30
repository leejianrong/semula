# Run it as a service on your laptop

If you work on several projects, one always-on semula that all of them share is easier than starting one per project. The repository has a Compose setup for exactly that in [`deploy/laptop/`](https://github.com/leejianrong/semula/tree/main/deploy/laptop).

```bash
cd deploy/laptop
docker compose up -d --wait
curl localhost:8731/health
```

```json
{"status":"ok","version":"0.4.0","mode":"replay"}
```

It runs the published image, keeps recordings in a Docker volume, and starts again after a reboot. The port is fixed at `8731` on loopback (change it with `SEMULA_PORT`).

## Three ways in

| From | Address |
|---|---|
| A process on this machine: your app, a test runner, an agent | `http://127.0.0.1:8731/v1` |
| A browser, through the shared Traefik proxy | `http://semula.localhost/ui/` |
| Another container on the `proxy` network | `http://semula:8080/v1` |

The browser and container routes need the Traefik layer, which only works if you already run a machine-wide Traefik that owns port 80 and the external `proxy` network:

```bash
docker compose -f compose.yaml -f compose.traefik.yaml up -d --wait
```

The direct port stays published, so both ways work at once. Without Traefik, leave the second file out.

!!! note "`*.localhost` is the browser's"
    Browsers resolve `semula.localhost`; `curl` and most language runtimes do not. From those, use the port, or add `127.0.0.1 semula.localhost` to `/etc/hosts`.

## Record through a real model

Normally a miss waits for a coding agent to answer it. To capture a real model's answers once and replay them for free, point semula at a provider and let one project forward its misses:

```bash
SEMULA_UPSTREAM_KEY=sk-... semula serve        # optionally SEMULA_UPSTREAM_URL for a compatible provider
semula project settings game --set '{"mode": "record", "record_through": true, "upstream_max_calls": 200}'
```

Copy `.env.example` to `.env` for the variables (it is git-ignored) and start with `uv run --env-file .env semula serve`. A miss in that project calls the provider once, saves the answer (chunks and usage if your app streams) and returns it. The same prompt replays from then on with no call. It only happens in `record` mode; replay mode fails a miss fast as always.

- The key lives only in the server's environment. It is never stored, logged, exported or shown in an error.
- `upstream_max_calls` (default 100) is a hard ceiling per project. A miss after it fails with `semula_upstream_budget`; recorded answers keep replaying. A failed call does not count.
- `upstream_max_tokens` caps prompt plus completion tokens too. Tokens are known after a call, so one call can overshoot; the next miss fails.
- Dry run first: `--set '{"upstream_dry_run": true}'` forwards nothing, fails each miss with `semula_upstream_dry_run`, and `semula project upstream <id>` lists the prompts that would have been called and how often. Turn it off (`null`) to forward for real.
- `semula project upstream <id>` shows calls and tokens against the caps; `--reset` counts from zero. The log page of the dashboard shows forwarded and upstream calls.
- `upstream_model` sends a different model upstream than the app asked for.
- Forwarding spends the operator's money. On a hosted server a workspace admin can turn it on only if the operator set `SEMULA_UPSTREAM_TENANTS=on`.

## Let a command answer each call

An agent answering many calls in one context sees the earlier prompts, which spoils an eval, and it cannot run unattended. The answerer hook starts one fresh process per parked call instead. The command is yours, set in the server's environment, and the request goes to it on stdin as JSON:

```bash
export SEMULA_ANSWERER_COMMAND='["claude","-p","You are the model the app calls. Answer as it would, in the format its prompt asks for. For a request with tools, reply only with JSON as in shape."]'
semula serve &
semula project settings game --set '{"mode": "record", "answerer": true, "max_misses": 200}'
```

What it does, call by call:

- Each call gets its own process in its own empty temporary directory, with `HOME`, `PATH` and a few basics in its environment and nothing else (never a `SEMULA_` variable, so your upstream key and API key stay out of reach). Name more with `SEMULA_ANSWERER_ENV`, for example `ANTHROPIC_API_KEY`.
- stdin is `{"request": {messages, tools, params}, "shape": {...}, "project", "pending_id", "attempt"}`. `shape` is the same view `semula show` gives: each tool's schema and a skeleton call. stdout is the answer: JSON that `semula answer` would take, or plain text for a plain reply (one code fence around the JSON is removed).
- An answer is checked like any other. If it is rejected, the command is run once more with `rejection` and `previous_output` added; a second rejection leaves the call parked.
- A failure, a timeout (`SEMULA_ANSWERER_TIMEOUT`, 120 s; the whole process group is killed), no output, or more than 1 MiB of output leaves the call parked for an agent or a person to answer. Nothing is retried while the server runs, so a broken command cannot loop; restarting tries again.
- While it works the hook holds the call, so `semula claim` skips it. At most `SEMULA_ANSWERER_CONCURRENCY` (2) run at once.
- `SEMULA_ANSWERER_URL` does the same with a webhook: one POST of that JSON, the answer in the response body, redirects not followed. Add `SEMULA_ANSWERER_BEARER` if it needs a token.

Read this before turning it on:

- **Set `max_misses`.** An app that parks hundreds of calls starts hundreds of processes, and with `claude -p` each is a model call.
- **A parked prompt is untrusted text** and may try to instruct your answerer. semula never runs what the command returns (it is data, validated and stored), but the command itself reads the prompt, so what it can do is what you let it do. Run the model with tools off; an answerer needs none. semula gives the child an empty directory and a stripped environment, not a sandbox.
- **The command is the operator's.** It is never a project setting, never read from a request, and turning the switch on needs the admin role and is audited. A hosted server refuses it.
- Needs a POSIX system (it uses process groups). The project's tests use a stand-in script; none runs a real model, so check your own `claude -p` setup on one call first.

## Stop a runaway loop

A loop in the app can park hundreds of calls and cost an agent hours. `max_misses` caps the distinct misses a project takes in one run:

```bash
semula project settings game --set '{"max_misses": 50}'
```

The 51st new miss fails with 429 `semula_miss_ceiling` and is not parked; it shows as a miss in the log. A call that joins one already parked is not a new miss, and replay hits are never counted. Set `max_misses` to a new value to start a new run, or to `null` to remove the ceiling.

## Read a long game as one conversation

An agent loop resends a growing history, so each parked call looks unrelated to the last. `semula conversations` groups calls whose messages extend an earlier call's, and shows what each one added:

```bash
semula conversations --project game          # parked calls, groups of two or more
semula conversations --log                   # logged calls (those that parked, and exact hits)
```

Grouping is by prefix only: a call joins the group of the earlier call whose messages are the longest prefix of its own, with the same tools. Prompts that merely look alike stay apart. `semula show <id>` and the dashboard show the same for one call (the group, how deep it is, and the messages added since the previous call). The log keeps only a preview of each call, so a logged call is placed by the request it points at; near matches and misses that failed fast are left out. Only the newest 500 calls of a project are looked at.

## Several workers on one queue

`semula claim` takes the oldest parked call nobody holds and prints it in full (messages, tools, and the answer `shape`), holding it for a lease so another worker does not get it:

```bash
semula claim --worker alice --lease 120     # prints one call, or "nothing to claim"
semula answer <id> --text "..."             # before the lease ends
```

Two workers never get the same call. If a worker dies, its lease runs out (default 300 s, at most 3600) and the call can be claimed again. The lease is advisory: it only keeps other claims away, so a person can still answer the call in the dashboard. MCP has `claim_next` and REST has `POST /api/v1/pending/claim`; the pending list shows who holds a call and until when.

## Give each project its own recordings

Create one semula project per app, set the project in the URL (`/v1/projects/<id>`) or with the `X-Semula-Project` header, and keep each app's mode separate. See [One project per app](../tutorial/projects.md).
