# semula on your own server

Caddy (automatic HTTPS) in front of the published semula image, with one API key and the data in a
volume. It is the plain server, not hosted mode: no sign-in, no tenants. For several teams with
sign-in and roles, use [`../droplet`](../droplet/README.md). The full walkthrough, with the
checklist for exposing a server, is in [the self-hosting guide](../../docs/guide/self-host.md).

You need Docker with compose, a name that points at the machine, and ports 80 and 443 open.

```bash
cd deploy/selfhost
./init.sh semula.example.com     # writes .env with a fresh API key (mode 600, never commit it)
docker compose up -d --wait
curl https://semula.example.com/health
```

Point an app at `https://semula.example.com/v1` with the key from `.env` as its API key. Agents and
the CLI read the key from `SEMULA_API_KEY`.

| File | What it is |
|---|---|
| `compose.yaml` | Caddy and semula on SQLite. Refuses to start with no API key. |
| `compose.postgres.yaml` | Overlay for Postgres: `docker compose -f compose.yaml -f compose.postgres.yaml up -d --wait` |
| `Caddyfile` | TLS, security headers, and a proxy that never buffers or times out a held call |
| `.env.example` | Every setting, with what it does. `init.sh` fills in the secrets. |

Upgrade: set `SEMULA_VERSION` in `.env` to the new tag, then `docker compose pull && docker compose up -d --wait`.
The server migrates its own database on start.

With a key set, the dashboard is off, because a browser cannot send the key on a page load.
Use the CLI, MCP or REST, or hosted mode if you want a dashboard behind sign-in.

Try it on your laptop first: `SEMULA_DOMAIN=localhost` makes Caddy use its own certificate
authority (your browser will warn). `make selfhost-check` from the repo root does this on
throwaway ports and volumes and checks the whole path.
