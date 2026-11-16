#!/usr/bin/env bash
# Bring up deploy/selfhost on a throwaway name and prove what a self-hoster is promised: it refuses
# to start with no API key, serves HTTPS with HSTS through Caddy, answers 401 with no key and 200
# with it, publishes only ports 80 and 443, takes a call in record mode and an agent's answer for
# it, replays that answer without waiting, and keeps it across a restart. With `postgres` as the
# second argument it adds compose.postgres.yaml (the image must be built with the postgres extra).
# Usage: scripts/smoke_selfhost.sh <image, like semula:ci> [sqlite|postgres]
# Needs Docker with compose, curl and python3.
set -euo pipefail

IMAGE="${1:?usage: smoke_selfhost.sh <image> [sqlite|postgres]}"
VARIANT="${2:-sqlite}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$HERE/deploy/selfhost"
PROJECT="semula-selfhost-smoke-$VARIANT-$$"
ENVFILE="$(mktemp)"
HTTP_PORT="$(python3 "$HERE/scripts/free_port.py" 18180)"
HTTPS_PORT="$(python3 "$HERE/scripts/free_port.py" $((HTTP_PORT + 1)))"
KEY="$(openssl rand -hex 16)"
# compose.yaml builds the image name from a repository and a version; split IMAGE at its last colon.
REPO="${IMAGE%:*}"; VERSION="${IMAGE##*:}"
FILES=(-f "$DIR/compose.yaml")
cat >"$ENVFILE" <<ENV
SEMULA_DOMAIN=localhost
SEMULA_API_KEY=$KEY
SEMULA_MODE=record
SEMULA_REPO=$REPO
HTTP_PORT=$HTTP_PORT
HTTPS_PORT=$HTTPS_PORT
ENV
if [ "$VARIANT" = postgres ]; then
  # the overlay appends -hosted to the version; the smoke image carries the driver under its own tag
  FILES+=(-f "$DIR/compose.postgres.yaml")
  printf 'SEMULA_VERSION=%s\nPOSTGRES_PASSWORD=%s\n' "${VERSION%-hosted}" "$(openssl rand -hex 16)" >>"$ENVFILE"
else
  printf 'SEMULA_VERSION=%s\n' "$VERSION" >>"$ENVFILE"
fi
compose() { docker compose -p "$PROJECT" --env-file "$ENVFILE" "${FILES[@]}" "$@"; }
cleanup() { compose down -v --remove-orphans >/dev/null 2>&1 || true; rm -f "$ENVFILE"; }
trap cleanup EXIT
fail() { echo "FAIL: $*"; compose logs --no-color --tail 40 2>&1 | tail -80; exit 1; }
base="https://localhost:$HTTPS_PORT"
auth=(-H "Authorization: Bearer $KEY")

# No key, no start: a server must never come up open by accident.
grep -v '^SEMULA_API_KEY=' "$ENVFILE" >"$ENVFILE.nokey"
docker compose -p "$PROJECT-nokey" --env-file "$ENVFILE.nokey" "${FILES[@]}" config -q >/dev/null 2>&1 &&
  { rm -f "$ENVFILE.nokey"; fail "compose accepted a missing SEMULA_API_KEY"; }
rm -f "$ENVFILE.nokey"

compose up -d --wait --wait-timeout 180 >/dev/null || fail "the stack did not come up"
for _ in $(seq 1 60); do curl -ksf "$base/health" >/dev/null 2>&1 && break; sleep 1; done
curl -ksf "$base/health" | grep -q '"status":"ok"' || fail "/health over https"
curl -ksI "$base/health" | grep -qi '^strict-transport-security: max-age=' || fail "no HSTS header"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$HTTP_PORT/health")
case "$code" in 301|302|307|308) ;; *) fail "http answered $code, not a redirect to https" ;; esac

[ "$(curl -ks -o /dev/null -w '%{http_code}' "$base/v1/models")" = 401 ] || fail "no key was not 401"
[ "$(curl -ks -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer wrong' "$base/v1/models")" = 401 ] || fail "a wrong key was not 401"
[ "$(curl -ks -o /dev/null -w '%{http_code}' "${auth[@]}" "$base/v1/models")" = 200 ] || fail "the key was not accepted"

published=$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}} {{.Ports}}' |
  grep -- '->' | grep -v caddy || true)
[ -z "$published" ] || fail "a container other than caddy publishes a port: $published"

# Record mode: an unknown prompt waits for an agent; the agent answers; the app gets it.
body='{"model":"m","messages":[{"role":"user","content":"selfhost smoke ping"}]}'
out="$ENVFILE.call"
curl -ks --max-time 60 -X POST "$base/v1/chat/completions" "${auth[@]}" -H 'Content-Type: application/json' -d "$body" >"$out" &
call=$!
pending=""
for _ in $(seq 1 30); do
  pending=$(curl -ksf "${auth[@]}" "$base/api/v1/pending?wait=2" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4 || true)
  [ -n "$pending" ] && break
done
[ -n "$pending" ] || fail "the call never parked"
curl -ksf -X POST "$base/api/v1/pending/$pending/answer" "${auth[@]}" -H 'Content-Type: application/json' \
  -d '{"content":"pong from the agent"}' >/dev/null || fail "answering the parked call"
wait "$call" || fail "the held call did not complete"
grep -q 'pong from the agent' "$out" || fail "the app did not get the agent's answer: $(cat "$out")"
rm -f "$out"

# Replay: the same prompt now answers at once with no agent, and survives a restart.
replay() { curl -ks --max-time 10 -X POST "$base/v1/chat/completions" "${auth[@]}" -H 'Content-Type: application/json' -d "$body"; }
replay | grep -q 'pong from the agent' || fail "the recorded answer did not replay"
compose restart semula >/dev/null
for _ in $(seq 1 60); do curl -ksf "$base/health" >/dev/null 2>&1 && break; sleep 1; done
replay | grep -q 'pong from the agent' || fail "the recording is gone after a restart"

if [ "$VARIANT" = postgres ]; then
  compose exec -T db psql -U semula -d semula -tAc "select count(*) from entry" | grep -qv '^0$' ||
    fail "the recording is not in Postgres"
else
  compose exec -T semula python -c 'import sqlite3; print(sqlite3.connect("/data/semula.db").execute("select count(*) from entry").fetchone()[0])' |
    grep -qv '^0$' || fail "the recording is not in the SQLite file"
fi
echo "ok ($VARIANT): $IMAGE behind Caddy with TLS and HSTS, key enforced, only 80/443 published, record then replay, kept across a restart"
