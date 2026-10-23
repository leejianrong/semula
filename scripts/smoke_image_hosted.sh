#!/usr/bin/env bash
# Prove the hosted image (built with the `hosted` and `postgres` extras) does what hosting needs:
# the sign-in and Postgres drivers import, a hosted server refuses a call with no token, and a
# token minted with `semula admin` in the same data volume is accepted.
# Usage: scripts/smoke_image_hosted.sh <image>      Needs Docker and curl.
set -euo pipefail

IMAGE="${1:?usage: smoke_image_hosted.sh <image>}"
NAME="semula-hosted-smoke-$$"
VOLUME="$NAME-data"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm "$VOLUME" >/dev/null 2>&1 || true' EXIT

docker run --rm "$IMAGE" python -c "import jwt, psycopg" \
  || { echo "FAIL: the hosted image lacks PyJWT or psycopg"; exit 1; }

# One run mints the workspace and a token into the volume; the server then reads it.
docker run --rm -v "$VOLUME:/data" -e SEMULA_HOSTED=on "$IMAGE" \
  semula admin workspace-create smoke >/dev/null
secret=$(docker run --rm -v "$VOLUME:/data" -e SEMULA_HOSTED=on "$IMAGE" \
  semula admin token-create --workspace smoke --name smoke --scope replay | grep -o 'smla_live_[A-Za-z0-9]*')
[ -n "$secret" ] || { echo "FAIL: no token was minted"; exit 1; }

docker run -d --name "$NAME" -p 127.0.0.1::8080 -v "$VOLUME:/data" -e SEMULA_HOSTED=on "$IMAGE" >/dev/null
port=$(docker port "$NAME" 8080/tcp | head -1 | sed 's/.*://')
base="http://127.0.0.1:${port}"
for _ in $(seq 1 100); do
  curl -fs "$base/health" >/dev/null 2>&1 && break
  sleep 0.2
done
curl -fs "$base/health" | grep -q '"status":"ok"' || { echo "FAIL: /health"; docker logs "$NAME"; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' "$base/v1/models")
[ "$code" = 401 ] || { echo "FAIL: a call with no token returned $code, not 401"; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $secret" "$base/v1/models")
[ "$code" = 200 ] || { echo "FAIL: the minted token returned $code, not 200"; docker logs "$NAME"; exit 1; }
echo "ok: $IMAGE has the sign-in and Postgres drivers, refuses a call with no token and accepts a minted one"
