# semula as a service on your laptop

One semula, always on, that every project on this machine shares. It runs the published image
from `ghcr.io/leejianrong/semula`, keeps its recordings in a Docker volume, and starts again after
a reboot.

```bash
cd deploy/laptop
docker compose up -d --wait        # pulls the image and starts it
curl localhost:8731/health         # {"status":"ok", ...}
```

## Getting the image

The image is public on ghcr.io once the first release is out, so there is nothing to log in to.
If the package is still private, log in once with a token that can read packages:

```bash
gh auth token | docker login ghcr.io -u <your-github-user> --password-stdin
```

To run a different version, set `SEMULA_IMAGE`, for example
`SEMULA_IMAGE=ghcr.io/leejianrong/semula:0.1.0 docker compose up -d --wait`. To use an image you
built yourself, set it to that tag.

## Three ways in

| From | Address | Notes |
|---|---|---|
| A process on this machine (your app, a test runner, an agent) | `http://127.0.0.1:8731/v1` | Fixed port, loopback only. Change it with `SEMULA_PORT`. Always works. |
| A browser | `http://semula.localhost/ui/` | Needs the Traefik layer below. |
| Another container | `http://semula:8080/v1` | Needs the Traefik layer: both containers join the `proxy` network. |

`*.localhost` is resolved by browsers, not by the system, so `curl`, scripts and most language
runtimes cannot resolve `semula.localhost`. Use the port from those, or add
`127.0.0.1 semula.localhost` to `/etc/hosts`.

### The Traefik layer

If the machine-wide proxy from the `traefik-dev-proxy` setup is running (it owns port 80 and the
external `proxy` network), add the second file:

```bash
docker compose -f compose.yaml -f compose.traefik.yaml up -d --wait
```

The direct port stays published, so both ways work at once. Without the proxy, leave the second
file out: `compose.yaml` stands on its own.

## Using it from a project

Give each project its own name, so recordings stay apart. The project is created on first use of
its name through the API (`semula project create my-app`), and every call carries it in the URL:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8731/v1/projects/my-app
export OPENAI_API_KEY=anything
```

By default semula is in **replay**: a prompt it has not recorded fails fast with an error, so a
test never hangs. To record, switch that one project (the choice is kept across restarts):

```bash
docker exec semula semula project create my-app
docker exec semula semula mode record --project my-app
docker exec semula semula pending                 # what is waiting for an answer
docker exec semula semula answer <id> --text "..."
docker exec semula semula mode replay --project my-app
```

A coding agent answers over MCP instead; register the endpoint once:

```bash
claude mcp add --transport http semula http://127.0.0.1:8731/mcp/
```

The dashboard is at `http://semula.localhost/ui/` (or `http://127.0.0.1:8731/ui/`).

## Upgrade, stop, back up

```bash
docker compose pull && docker compose up -d --wait    # newer image; the database upgrades itself
docker compose down                                    # stop; recordings stay in the volume
docker run --rm -v semula_semula-data:/data -v "$PWD":/backup busybox \
  tar czf /backup/semula-data.tgz -C /data .           # a backup of everything
```

`docker compose down -v` deletes the volume, and with it every recording.

## If something is off

- **Port already in use:** `SEMULA_PORT=8788 docker compose up -d --wait`.
- **`semula.localhost` does not load:** is Traefik up (`docker ps`) and did you add
  `compose.traefik.yaml`? If Traefik is up but routes nothing, see its logs
  (`docker logs dev-proxy-traefik-1`).
- **Calls fail with 424 in a project that should record:** that project is in replay. Check with
  `docker exec semula semula mode`.
- **Anything reachable beyond this machine:** do not change the `127.0.0.1` in the port mapping
  without setting `SEMULA_API_KEY`, which also turns the dashboard off.
