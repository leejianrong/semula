# Run it as a service on your laptop

If you work on several projects, one always-on semula that all of them share is easier than starting one per project. The repository has a Compose setup for exactly that in [`deploy/laptop/`](https://github.com/leejianrong/semula/tree/main/deploy/laptop).

```bash
cd deploy/laptop
docker compose up -d --wait
curl localhost:8731/health
```

```json
{"status":"ok","version":"0.2.0","mode":"replay"}
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

## Stop a runaway loop

A loop in the app can park hundreds of calls and cost an agent hours. `max_misses` caps the distinct misses a project takes in one run:

```bash
semula project settings game --set '{"max_misses": 50}'
```

The 51st new miss fails with 429 `semula_miss_ceiling` and is not parked; it shows as a miss in the log. A call that joins one already parked is not a new miss, and replay hits are never counted. Set `max_misses` to a new value to start a new run, or to `null` to remove the ceiling.

## Give each project its own recordings

Create one semula project per app, set the project in the URL (`/v1/projects/<id>`) or with the `X-Semula-Project` header, and keep each app's mode separate. See [One project per app](../tutorial/projects.md).
