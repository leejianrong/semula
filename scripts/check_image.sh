#!/usr/bin/env bash
# Budget check for the container image (SEM-38): size and cold start, and that it runs
# as a non-root user and persists data on /data. Needs Docker. Usage: scripts/check_image.sh [tag]
set -euo pipefail

TAG="${1:-semula:check}"
MAX_MB="${MAX_IMAGE_MB:-350}"
MAX_START_S="${MAX_START_SECONDS:-2}"
NAME="semula-image-check-$$"
VOLUME="semula-image-check-$$"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm "$VOLUME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker build -q -t "$TAG" . >/dev/null
size_mb=$(( $(docker image inspect "$TAG" --format '{{.Size}}') / 1000000 ))
echo "image size: ${size_mb} MB (budget ${MAX_MB} MB)"
[ "$size_mb" -le "$MAX_MB" ] || { echo "FAIL: image too large"; exit 1; }

user=$(docker run --rm --entrypoint id "$TAG" -u)
echo "runs as uid: ${user}"
[ "$user" != "0" ] || { echo "FAIL: container runs as root"; exit 1; }

# Cold start is measured from the container's own start time to the first healthy response,
# so Docker's create overhead on the host is not charged to semula.
docker run -d --name "$NAME" -v "$VOLUME:/data" -p 127.0.0.1::8080 "$TAG" >/dev/null
port=$(docker port "$NAME" 8080/tcp | head -1 | sed 's/.*://')
begun=$(date +%s.%N)
until curl -fs "http://127.0.0.1:${port}/health" >/dev/null 2>&1; do
  sleep 0.05
  if [ "$(echo "$(date +%s.%N) - $begun > 30" | bc)" = 1 ]; then echo "FAIL: never became healthy"; docker logs "$NAME"; exit 1; fi
done
started_at=$(docker inspect "$NAME" --format '{{.State.StartedAt}}')
elapsed=$(echo "$(date +%s.%N) - $(date -d "$started_at" +%s.%N)" | bc)
printf 'cold start: %.2f s (budget %s s)\n' "$elapsed" "$MAX_START_S"
[ "$(echo "$elapsed <= $MAX_START_S" | bc)" = 1 ] || { echo "FAIL: cold start too slow"; exit 1; }

# The dashboard is built in the background after start-up: /ui must come up, and a deep link
# (a pasted entry URL) must load the same shell.
for path in /ui/ /ui/projects/default/entries; do
  curl -fsL --max-time 15 "http://127.0.0.1:${port}${path}" | grep -q '<div id="app">' \
    || { echo "FAIL: ${path} did not serve the dashboard"; docker logs "$NAME"; exit 1; }
done
echo "ok: the dashboard is served at /ui, deep links included"

# Semantic matching is an opt-in extra: the default image must carry neither the library nor a model.
if docker run --rm --entrypoint python "$TAG" -c "import fastembed" >/dev/null 2>&1; then
  echo "FAIL: the default image contains fastembed"; exit 1
fi
docker run --rm --entrypoint sh "$TAG" -c 'test ! -e /data/models' \
  || { echo "FAIL: the default image contains a model directory"; exit 1; }
echo "ok: no embeddings library or model in the default image"

# The volume is the only place data lives: a project survives the container being replaced.
curl -fs -X POST "http://127.0.0.1:${port}/api/v1/projects" -H 'content-type: application/json' \
  -d '{"id":"persisted"}' >/dev/null
docker rm -f "$NAME" >/dev/null
docker run -d --name "$NAME" -v "$VOLUME:/data" -p 127.0.0.1::8080 "$TAG" >/dev/null
port=$(docker port "$NAME" 8080/tcp | head -1 | sed 's/.*://')
for _ in $(seq 1 200); do curl -fs "http://127.0.0.1:${port}/health" >/dev/null 2>&1 && break; sleep 0.1; done
curl -fs "http://127.0.0.1:${port}/api/v1/projects/persisted" >/dev/null \
  || { echo "FAIL: data did not survive a restart on the /data volume"; exit 1; }
echo "ok: data persisted on /data"
