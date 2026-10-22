#!/usr/bin/env bash
# Start an image and prove it works the way a user would meet it: /health, the models list, an
# unknown prompt failing fast in replay mode, and the dashboard (deep link included).
# Usage: scripts/smoke_image.sh <image>      Needs Docker and curl.
set -euo pipefail

IMAGE="${1:?usage: smoke_image.sh <image>}"
NAME="semula-smoke-$$"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

docker run -d --name "$NAME" -p 127.0.0.1::8080 "$IMAGE" >/dev/null
port=$(docker port "$NAME" 8080/tcp | head -1 | sed 's/.*://')
base="http://127.0.0.1:${port}"

for _ in $(seq 1 100); do
  curl -fs "$base/health" >/dev/null 2>&1 && break
  sleep 0.2
done
curl -fs "$base/health" | grep -q '"status":"ok"' || { echo "FAIL: /health"; docker logs "$NAME"; exit 1; }
curl -fs "$base/health" | grep -o '"version":"[^"]*"'

curl -fs "$base/v1/models" | grep -q '"object":"list"' || { echo "FAIL: /v1/models"; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$base/v1/chat/completions" \
  -H 'content-type: application/json' \
  -d '{"model":"m","messages":[{"role":"user","content":"never recorded"}]}')
[ "$code" = 424 ] || { echo "FAIL: an unknown prompt returned $code, not 424"; exit 1; }

for path in /ui/ /ui/projects/default/log; do
  curl -fsL --max-time 20 "$base$path" | grep -q '<div id="app">' \
    || { echo "FAIL: $path did not serve the dashboard"; docker logs "$NAME"; exit 1; }
done
echo "ok: $IMAGE answers /health, /v1/models, fails fast on a miss and serves /ui"
