#!/usr/bin/env bash
# The embeddings image (SEM-40): built with the extra, the model is fetched once onto the data
# volume, and after that the server starts with no network at all. Needs Docker and, for the
# first fetch, network access. Usage: scripts/check_embeddings_image.sh [tag]
set -euo pipefail

TAG="${1:-semula:embeddings-check}"
NAME="semula-embed-check-$$"
VOLUME="semula-embed-check-$$"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm "$VOLUME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker build -q -t "$TAG" --build-arg UV_EXTRAS="--extra embeddings" . >/dev/null
echo "image size: $(( $(docker image inspect "$TAG" --format '{{.Size}}') / 1000000 )) MB (the default image is ~240 MB)"

echo "first run: fetching the model onto the volume (needs network)"
docker run --rm -e SEMULA_DATA_DIR=/data -v "$VOLUME:/data" "$TAG" semula embeddings fetch

echo "second run: no network, offline flag set"
docker run -d --name "$NAME" --network none -v "$VOLUME:/data" \
  -e SEMULA_EMBEDDINGS=on -e SEMULA_EMBEDDINGS_OFFLINE=on "$TAG" >/dev/null
for _ in $(seq 1 100); do
  if docker exec "$NAME" python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/health', timeout=2)" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
status=$(docker exec "$NAME" python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/api/v1/embeddings', timeout=5).read().decode())") \
  || { echo "FAIL: the server did not start offline"; docker logs "$NAME"; exit 1; }
echo "embeddings status: $status"
echo "$status" | grep -q '"enabled":true' || { echo "FAIL: embeddings are not enabled"; exit 1; }
echo "ok: starts offline after the first download"
