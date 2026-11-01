# semula, hosted, on one droplet

One compose project: **Caddy** (TLS from Let's Encrypt, HSTS) in front of **semula** (the hosted
image) on **Postgres 16**, a **migrate** step that runs before the app, a daily **purge** of deleted
workspaces, and an **admin** service for operator commands. Only ports 80 and 443 are published.

The app connects as the Postgres role `semula`, which owns no table and bypasses nothing, so
row-level security applies to it. Migrations, the purge and `admin` connect as `semula_owner`
(see "Postgres and roles" in `docs/guide/hosted.md`).

## Two variants: Postgres, or SQLite on a small droplet

| | `compose.yaml` | `compose.sqlite.yaml` |
|---|---|---|
| Database | Postgres 16 in the same project, app on a role with row-level security | one SQLite file in the `data` volume |
| Fits | a 2 GB droplet or more | a 1 GB droplet (add swap: `fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile`) |
| App containers | one or more | exactly one (one writer, in-memory event bus) |
| Tenant isolation | the service's filter, and RLS behind it | the service's filter alone |

Everything else (Caddy, headers, the trusted proxy, the purge, `admin`) is the same. Start the SQLite
variant with `docker compose -f compose.sqlite.yaml up -d --wait`, and use the same `-f` on every
later command. To move to Postgres once you outgrow it, see "SQLite or Postgres" in
`docs/guide/hosted.md`; until the copy tool exists (SEM-127) that means starting a new database.

## First start

Needs Docker with compose, a name that resolves to the machine, and ports 80 and 443 open.

```bash
cd deploy/droplet
cp .env.example .env            # then fill it in; make each secret with `openssl rand -hex 32`
docker compose up -d --wait
curl https://semula.example.com/health
```

Operator commands run as the database owner:

```bash
docker compose run --rm admin workspace-create acme --name "Acme"
docker compose run --rm admin invite --workspace acme --email you@example.com --role admin
docker compose run --rm admin token-create --workspace acme --name ci --scope replay
```

Upgrade: set `SEMULA_IMAGE` to the new tag, then `docker compose pull && docker compose up -d --wait`
(the migrate step runs first). Logs: `docker compose logs -f semula caddy`.

## Before it is public

- **The image has to include `SEMULA_TRUSTED_PROXIES`** (after 0.4.0). An older image ignores it, and
  then every user looks like Caddy and twenty failed guesses from anyone lock everyone out. Pin a newer tag.
- **Certificates live in the `caddy-data` volume.** Losing it re-issues them, which Let's Encrypt rate limits.
- **Back it up.** `pgdata` (or the `data` volume on SQLite) is the only copy of the data until you add the backup service (`backup/`, see "Automatic backups" in `docs/guide/upgrade-and-backup.md`): encrypted daily dumps, off-site to a bucket, kept 14 days, with an alert when one is missed. Turn on droplet snapshots too.
- Try the whole stack without a domain: `SEMULA_DOMAIN=localhost` makes Caddy use its own CA, and
  `make droplet-smoke` does exactly that on throwaway ports and volumes.

## Why it is shaped like this

- Caddy has the one fixed address (`172.30.0.2`) and semula trusts `X-Forwarded-For` from that address
  alone. Caddy replaces any header a client sends, so a client cannot choose the address that rate
  limits and the audit log see.
- A parked call is held until an agent answers (up to five minutes), and dashboard event streams stay
  open, so Caddy never buffers a response and has no read timeout.
- Each container has a memory limit that fits a 2 GB droplet (Postgres 512 MB, the app 768 MB). They
  are first guesses; the load test card sets them from measurements.
