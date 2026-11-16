# semula Helm chart

Runs semula on Kubernetes: one Deployment, a Service, a volume for SQLite (or a Postgres), an API key
the chart generates, and an optional Ingress. The image is the published one, `ghcr.io/leejianrong/semula`
(a fork sets `image.repository`).

```bash
helm install semula ./deploy/helm/semula -n semula --create-namespace
kubectl -n semula get secret semula-auth -o jsonpath='{.data.api-key}' | base64 -d   # the key
kubectl -n semula port-forward svc/semula 8080:80
curl http://127.0.0.1:8080/health
```

From a clone this chart is `deploy/helm/semula`. `helm show values ./deploy/helm/semula` lists every
setting. The walkthrough, with the checklist for exposing a server, is in
[the self-hosting guide](../../../docs/guide/self-host.md).

## The choices that matter

| You want | Set | Because |
|---|---|---|
| The simplest server | nothing | SQLite on a 5 Gi volume, one replica, a generated key |
| To keep the key out of Helm's history | `auth.existingSecret` | a key in `auth.apiKey` is stored in the release |
| More than one replica, or rolling updates | `postgresql.enabled=true` or `externalDatabase` | SQLite has one writer, so it runs one pod and restarts it on upgrade |
| Your own Postgres | `externalDatabase.existingSecret` (a Secret holding the URL) | the password stays out of values |
| It reachable from outside | `ingress.enabled`, `publicUrl`, `trustedProxies` | see below |
| Record-through to a provider | `envFrom` a Secret with `SEMULA_UPSTREAM_KEY`, `env.SEMULA_UPSTREAM_URL` | the key is never a chart value |

**Postgres uses the `-hosted` image.** The Postgres driver is in that variant, which runs as the plain
server unless `SEMULA_HOSTED=on`. The chart adds the suffix for you.

**Behind an Ingress**, a parked call is held until an agent answers (up to five minutes) and streams stay
open, so the controller must not buffer or time out early (the commented annotations in `values.yaml`).
Set `trustedProxies` to the controller's pod range, or every client looks like the controller to the
rate limiter and the log.

**The dashboard is off while a key is set**, because a browser cannot send the key on a page load. Use the
CLI, MCP or REST.

**Data outlives `helm uninstall`.** The SQLite claim is annotated `helm.sh/resource-policy: keep`. Delete it
yourself to drop the recordings: `kubectl -n semula delete pvc semula`. The bundled Postgres keeps its claim
too (a StatefulSet never deletes one): `kubectl -n semula delete pvc data-semula-postgresql-0`.

## Not covered yet: hosted mode

Hosted mode (sign-in, tenants) needs two Postgres roles so row-level security applies, and migrations run
as the owner. `deploy/droplet` does that for Docker Compose. This chart does not, so it runs the plain
server only.

## Checking the chart

```bash
make chart-check      # lint and render every values file in ci/, and check each file in ci-fail/ is refused
make chart-smoke      # install into a new namespace on the cluster kubectl points at, prove it, remove it
```

`chart-smoke` creates the namespace `semula-test` (it refuses one that exists) and deletes only that. It
checks the key, a held call that an agent answers, a replay that survives a pod restart, `helm upgrade` and
`helm rollback`, and that uninstall keeps the data. CI runs it on a k3d cluster for SQLite and Postgres.
To run it with an image you built, `HELM_ARGS="--set image.repository=semula --set image.tag=dev --set image.pullPolicy=Never"`
(import it first: `k3d image import semula:dev -c <cluster>`).
