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

## Give each project its own recordings

Create one semula project per app, set the project in the URL (`/v1/projects/<id>`) or with the `X-Semula-Project` header, and keep each app's mode separate. See [One project per app](../tutorial/projects.md).
