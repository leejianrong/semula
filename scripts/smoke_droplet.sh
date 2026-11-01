#!/usr/bin/env bash
# Bring up deploy/droplet as a whole, on a throwaway name, and prove what the deployment is for:
# TLS and HSTS from Caddy, the app refusing a call with no token, nothing published but 80 and
# 443, the app connected as a role that owns nothing and bypasses nothing, row-level security on,
# one workspace unable to see another's project through the real stack, and a client unable to
# choose the address semula records by writing X-Forwarded-For.
# With `sqlite` as the second argument it checks deploy/droplet/compose.sqlite.yaml instead: the
# same checks, minus the Postgres role and row-level security ones, plus the database file
# surviving a restart and many writes at once never hitting "database is locked".
# Usage: scripts/smoke_droplet.sh <hosted image> [postgres|sqlite]
# Needs Docker with compose, curl and python3.
set -euo pipefail

IMAGE="${1:?usage: smoke_droplet.sh <hosted image> [postgres|sqlite]}"
VARIANT="${2:-postgres}"
case "$VARIANT" in postgres) FILE=compose.yaml ;; sqlite) FILE=compose.sqlite.yaml ;;
  *) echo "variant is postgres or sqlite" >&2; exit 2 ;; esac
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$HERE/deploy/droplet"
PROJECT="semula-droplet-smoke-$VARIANT-$$"
ENVFILE="$(mktemp)"
HTTP_PORT="$(python3 "$HERE/scripts/free_port.py" 18080)"
HTTPS_PORT="$(python3 "$HERE/scripts/free_port.py" $((HTTP_PORT + 1)))"
cat >"$ENVFILE" <<ENV
SEMULA_DOMAIN=localhost
SEMULA_IMAGE=$IMAGE
SEMULA_SECRET=$(openssl rand -hex 32)
HTTP_PORT=$HTTP_PORT
HTTPS_PORT=$HTTPS_PORT
ENV
if [ "$VARIANT" = postgres ]; then
  printf 'POSTGRES_OWNER_PASSWORD=%s\nSEMULA_DB_PASSWORD=%s\n' "$(openssl rand -hex 16)" "$(openssl rand -hex 16)" >>"$ENVFILE"
fi
compose() { docker compose -p "$PROJECT" --env-file "$ENVFILE" -f "$DIR/$FILE" "$@"; }
cleanup() { compose down -v --remove-orphans >/dev/null 2>&1 || true; rm -f "$ENVFILE"; }
trap cleanup EXIT
fail() { echo "FAIL: $*"; compose logs --no-color --tail 40 2>&1 | tail -80; exit 1; }
sql() {
  if [ "$VARIANT" = postgres ]; then
    compose exec -T db psql -U semula_owner -d semula -tAc "$1" | tr -d '[:space:]'
  else
    compose exec -T semula python -c 'import sqlite3,sys; print(sqlite3.connect("/data/semula.db").execute(sys.argv[1]).fetchone()[0])' "$1" | tr -d '[:space:]'
  fi
}
base="https://localhost:$HTTPS_PORT"

compose up -d --wait --wait-timeout 180 >/dev/null || fail "the stack did not come up"
for _ in $(seq 1 60); do curl -ksf "$base/health" >/dev/null 2>&1 && break; sleep 1; done
curl -ksf "$base/health" | grep -q '"status":"ok"' || fail "/health over https"
curl -ksI "$base/health" | grep -qi '^strict-transport-security: max-age=' || fail "no HSTS header"
for header in permissions-policy cross-origin-opener-policy cross-origin-resource-policy; do
  curl -ksI "$base/health" | grep -qi "^$header:" || fail "no $header header"
done

code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$HTTP_PORT/health")
case "$code" in 301|302|307|308) ;; *) fail "http answered $code, not a redirect to https" ;; esac
[ "$(curl -ks -o /dev/null -w '%{http_code}' "$base/v1/models")" = 401 ] || fail "no token was not 401"

# Secrets in an address stay out of Caddy's log (and the app's).
curl -ks -o /dev/null "$base/auth/callback/google?code=SECRETCODE123&state=SECRETSTATE123"
curl -ks -o /dev/null "$base/auth/invite/smla_inv_SECRETINVITE123"
curl -ks -o /dev/null "$base/ui/api/stream?sid=SECRETSID123&path=%2F"
sleep 1
logs=$(compose logs --no-color caddy semula 2>&1)
for secret in SECRETCODE123 SECRETSTATE123 SECRETINVITE123 SECRETSID123; do
  echo "$logs" | grep -q "$secret" && fail "$secret is in the logs"
done
echo "$logs" | grep -q '/auth/callback/google' || fail "the callback request is not in the log at all"

# Only Caddy publishes anything.
published=$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}} {{.Ports}}' |
  grep -- '->' | grep -v caddy || true)
[ -z "$published" ] || fail "a container other than caddy publishes a port: $published"

if [ "$VARIANT" = sqlite ]; then
  # One file in the data volume, in write-ahead mode, and nothing else holding the data.
  [ "$(sql "pragma journal_mode")" = wal ] || fail "the database is not in WAL mode"
  [ "$(sql "select count(*) from workspace")" -ge 0 ] || fail "the database file is not readable"
  [ "$(compose ps -q semula | wc -l)" = 1 ] || fail "more than one app container"
else
  # The app's role owns nothing and bypasses nothing; the tables have row-level security.
  [ "$(sql "select rolsuper::text||rolbypassrls::text from pg_roles where rolname='semula'")" = falsefalse ] ||
    fail "the app's role is a superuser or bypasses RLS"
  [ "$(sql "select count(*) from pg_tables where schemaname='public' and tableowner='semula'")" = 0 ] ||
    fail "the app's role owns tables"
  [ "$(sql "select count(*) > 0 from pg_class where relkind='r' and relrowsecurity and relnamespace='public'::regnamespace")" = t ] ||
    fail "no table has row-level security"
  [ "$(sql "select count(*) > 0 from pg_stat_activity where usename='semula'")" = t ] ||
    fail "the app is not connected as the semula role"
fi

# Two workspaces through the real stack: each sees only its own project.
admin() { compose run --rm -T admin "$@"; }
admin workspace-create a --name A >/dev/null
admin workspace-create b --name B >/dev/null
token() { admin token-create --workspace "$1" --name smoke --scope admin | grep -o 'smla_live_[A-Za-z0-9]*'; }
ta=$(token a); tb=$(token b)
[ -n "$ta" ] && [ -n "$tb" ] || fail "no token was minted"
curl -ksf -X POST "$base/api/v1/projects" -H "Authorization: Bearer $ta" \
  -H 'Content-Type: application/json' -d '{"id":"only-a"}' >/dev/null || fail "creating a project as the app role"
curl -ksf "$base/api/v1/projects" -H "Authorization: Bearer $ta" | grep -q only-a || fail "a cannot see its project"
curl -ksf "$base/api/v1/projects" -H "Authorization: Bearer $tb" | grep -q only-a &&
  fail "b can see a's project"

# The address semula records is the real client: not one a client writes into X-Forwarded-For,
# and not Caddy's own. (The audit log keeps the exact address.)
curl -ksf -X POST "$base/api/v1/projects" -H "Authorization: Bearer $tb" -H 'X-Forwarded-For: 9.9.9.9' \
  -H 'Content-Type: application/json' -d '{"id":"probe"}' >/dev/null || fail "creating the probe project"
seen=$(sql "select client from audit_log where action='project.create' and target='probe'")
caddy_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(compose ps -q caddy)")
[ -n "$seen" ] || fail "the audit log has no client address for the probe"
[ "$seen" != 9.9.9.9 ] || fail "X-Forwarded-For from a client was believed"
[ "$seen" != "$caddy_ip" ] || fail "the recorded address is the proxy's ($caddy_ip): the proxy is not trusted"
# Many writes at once all succeed (SQLite has one writer; a lock must wait, never fail).
pids=()
for i in $(seq 1 40); do
  curl -ks -o /dev/null -w '%{http_code}\n' -X POST "$base/api/v1/projects/only-a/entries" -H "Authorization: Bearer $ta" \
    -H 'Content-Type: application/json' \
    -d "{\"request\":{\"model\":\"m\",\"messages\":[{\"role\":\"user\",\"content\":\"burst $i\"}]},\"response\":{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"ok\"}}]}}" >"$ENVFILE.burst.$i" &
  pids+=($!)
done
wait "${pids[@]}"
codes=$(cat "$ENVFILE".burst.* | sort | uniq -c | tr '\n' ' ')
rm -f "$ENVFILE".burst.*
[ "$(echo "$codes" | grep -vc '^ *40 20[01] *$')" = 0 ] || fail "simultaneous writes: $codes"
compose logs --no-color semula 2>&1 | grep -qi 'database is locked' && fail "the app hit 'database is locked'"

# A restart loses nothing.
if [ "$VARIANT" = sqlite ]; then
  compose restart semula >/dev/null
  for _ in $(seq 1 60); do curl -ksf "$base/health" >/dev/null 2>&1 && break; sleep 1; done
  curl -ksf "$base/api/v1/projects" -H "Authorization: Bearer $ta" | grep -q only-a ||
    fail "a's project is gone after a restart"
fi
if [ "$VARIANT" = postgres ]; then db="the app on a role with RLS applied"; else db="one SQLite file in a volume that survives a restart"; fi
echo "ok ($VARIANT): $IMAGE behind Caddy with TLS and HSTS, nothing but 80/443 published, $db, tenants isolated, the client address real, 40 writes at once"
