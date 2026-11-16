# How to self-host semula

This guide takes you from a clone of the repository to a semula server other people and agents can reach, with HTTPS and an API key. Pick the path that matches where you run things:

| You have | Use | Database |
|---|---|---|
| One machine with Docker and a domain name | [Docker Compose](#docker-compose-on-a-server) | SQLite, or Postgres |
| A Kubernetes cluster | [The Helm chart](#kubernetes-with-helm) | SQLite on a volume, or Postgres |
| Several teams who need sign-in, roles and per-token limits | [Hosted mode](hosted.md) and [`deploy/droplet`](https://github.com/leejianrong/semula/tree/main/deploy/droplet) | Postgres or SQLite |

The first two run the plain server: one API key guards `/v1`, `/api` and `/mcp`, and anyone with the key can do anything. That is the right size for a team that trusts each other, a CI farm, or one person's always-on box. Everything below runs the published image, so you do not build anything.

!!! warning "With an API key, the dashboard is off"
    A browser cannot send the key on a page load, so the dashboard turns itself off whenever a key is set. Use the CLI, MCP or REST on a keyed server. For a dashboard behind sign-in, run [hosted mode](hosted.md).

## Docker Compose on a server

You need Docker with compose, a name that points at the machine (an `A` record), and ports 80 and 443 open to the internet. Caddy asks Let's Encrypt for a certificate on the first start.

```bash
git clone https://github.com/leejianrong/semula && cd semula/deploy/selfhost
./init.sh semula.example.com
docker compose up -d --wait
curl https://semula.example.com/health
```

```json
{"status":"ok","version":"0.6.0","mode":"record"}
```

`init.sh` writes `.env` with a fresh `SEMULA_API_KEY` (mode 600) and never replaces a value that is already there. Compose refuses to start without a key, so a server cannot come up open by mistake. Only Caddy publishes ports; semula itself is reachable only through it.

Point an app at the server with the key as its API key, and keep each app in its own [project](../tutorial/projects.md):

```python
from openai import OpenAI

client = OpenAI(base_url="https://semula.example.com/v1/projects/my-app", api_key="<SEMULA_API_KEY>")
```

Connect a coding agent to the MCP endpoint, sending the key as a header:

```bash
claude mcp add --transport http semula https://semula.example.com/mcp/ \
  --header "Authorization: Bearer $SEMULA_API_KEY"
```

The CLI reads the key from the environment: `SEMULA_API_KEY=... semula --url https://semula.example.com pending`.

### Use Postgres instead of SQLite

```bash
docker compose -f compose.yaml -f compose.postgres.yaml up -d --wait
```

`init.sh` already made the database password. Use the same two `-f` flags for every later command, or set `COMPOSE_FILE=compose.yaml:compose.postgres.yaml` in `.env`. The Postgres driver ships in the `-hosted` image, and the overlay picks it; it runs as the plain server unless you set `SEMULA_HOSTED=on`.

### Try it on your laptop first

Set the domain to `localhost` and Caddy uses its own certificate authority instead of Let's Encrypt (your browser warns about the certificate; `curl -k` skips it). From the repository root, `make selfhost-check` does this on throwaway ports and volumes and checks the whole path: no key means no start, HTTPS with HSTS, `401` without the key, only 80 and 443 published, a held call answered by an agent and then replayed, and the recording kept across a restart.

!!! note "What the tests do not cover"
    The check runs against Caddy's own certificate authority. It does not request a real Let's Encrypt certificate, so watch `docker compose logs caddy` on your first start. If your domain does not resolve to the machine yet, Caddy retries and logs why.

## Kubernetes with Helm

The chart is `deploy/helm/semula` in the repository. It runs one Deployment, a Service, a volume for SQLite (or a Postgres), and an API key it generates once and keeps across upgrades.

```bash
git clone https://github.com/leejianrong/semula && cd semula
helm install semula deploy/helm/semula -n semula --create-namespace
kubectl -n semula get secret semula-auth -o jsonpath='{.data.api-key}' | base64 -d
kubectl -n semula port-forward svc/semula 8080:80
curl http://127.0.0.1:8080/health
```

To make it reachable, turn on the Ingress and name the address people use:

```yaml
# values.yaml
publicUrl: https://semula.example.com
trustedProxies: 10.42.0.0/16        # your ingress controller's pod range
ingress:
  enabled: true
  className: nginx
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
  hosts:
    - host: semula.example.com
      paths: [{path: /, pathType: Prefix}]
  tls:
    - secretName: semula-tls
      hosts: [semula.example.com]
```

```bash
helm upgrade semula deploy/helm/semula -n semula -f values.yaml
```

Three settings matter more than the rest:

- **One replica on SQLite.** SQLite has one writer, so the chart runs one pod and restarts it on an upgrade instead of rolling. For more than one replica, set `postgresql.enabled=true` (a StatefulSet in the chart) or `externalDatabase.existingSecret` (your own database, with its URL in a Secret). The chart refuses `replicaCount` above 1 without Postgres.
- **The proxy must not cut a held call off.** A call semula has not seen waits for an agent to answer, up to five minutes, so the controller needs a long read timeout and no response buffering (the annotations above are for ingress-nginx).
- **Set `trustedProxies`.** Without it every client looks like the ingress controller, so the address semula logs and limits by is the controller's, not the caller's.

The chart's [README](https://github.com/leejianrong/semula/blob/main/deploy/helm/semula/README.md) lists every setting. `make chart-smoke` installs it into a new namespace on whatever cluster `kubectl` points at, checks the key, a held call that an agent answers, and a replay that survives a pod restart, `helm upgrade` and `helm rollback`, and then removes it. The same test runs in CI on a k3d cluster.

!!! note "Hosted mode is not in the chart"
    Hosted mode needs two Postgres roles so row-level security applies, and migrations that run as the owner. [`deploy/droplet`](https://github.com/leejianrong/semula/tree/main/deploy/droplet) does that for Docker Compose; the chart does not yet, so it runs the plain server only.

## Before you expose it

1. **A key is set.** Compose and the chart both insist. Keep it in a secrets store, not in a chat or a repository.
2. **Only the proxy is published.** Compose publishes Caddy's 80 and 443 and nothing else. In Kubernetes, leave the Service as `ClusterIP` and go through the Ingress.
3. **TLS ends at the proxy, and the proxy is trusted.** `SEMULA_TRUSTED_PROXIES` names the proxy's address, so semula believes `X-Forwarded-For` from it and from nothing else.
4. **Pin a version.** Set `SEMULA_VERSION` (Compose) or `image.tag` (Helm) to a release such as `0.6.0`. `latest` moves.
5. **Leave the answerer off.** `SEMULA_ANSWERER_COMMAND` runs a command you choose for each parked call. On a server other people can reach, leave it unset unless you have read [Let a command answer](answerer.md).
6. **Treat the data as sensitive.** A recorded prompt can contain whatever your app sent. Restrict who can read the volume and the backups.
7. **Back it up** (next section), including `.env`: without the key you cannot reach your own server.

## Back up and upgrade

[Upgrade and back up](upgrade-and-backup.md) covers what is stored, restoring a copy, and why there is no downgrade. For these setups:

- **Compose, SQLite.** Stop semula, tar the `semula_data` volume, start it again. The commands in that page work with the volume name `semula_data`.
- **Compose, Postgres.** `docker compose -f compose.yaml -f compose.postgres.yaml exec db pg_dump -U semula semula > semula.sql`.
- **Helm.** Snapshot the claim with your storage's tools, or write each project to files with `semula export <project> --dir ./out` through a port-forward.
- **Upgrade.** Set `SEMULA_VERSION` (or `image.tag`) to the new release, then `docker compose pull && docker compose up -d --wait`, or `helm upgrade`. The server migrates its own database on start. Back up first: a migrated database cannot be read by an older release.

## When something is wrong

| You see | Likely cause |
|---|---|
| Compose stops with `set SEMULA_API_KEY` | `.env` is missing or empty: run `./init.sh <domain>` |
| `curl https://...` hangs or the certificate is wrong | The name does not resolve to this machine yet, or port 80 is blocked: `docker compose logs caddy` |
| `401` on every call | The key in the request differs from `.env`. The CLI reads `SEMULA_API_KEY` from the environment |
| A call in record mode fails after a while, though an agent was about to answer | A proxy in front is timing out a held call: raise its read timeout and turn buffering off |
| The Helm pod restarts at once with `invalid literal for int()` | A Service links variables into the pod. The chart turns that off (`enableServiceLinks: false`); keep it |
| `403` at `/ui/` | The dashboard is off while a key is set. See the warning at the top |
