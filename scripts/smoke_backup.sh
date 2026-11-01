#!/usr/bin/env bash
# The restore drill (SEM-120), on the real compose stack: make data, back it up with the backup
# service, destroy the database volume (the lost droplet), start empty, restore from the dump
# alone, and show the data is back and the app serves it. Encrypted, so the passphrase is part of
# the drill. Prints how long the backup and the restore took, for the runbook.
# Usage: scripts/smoke_backup.sh <hosted image> [postgres|sqlite]
# Needs Docker with compose, curl and python3. Not run against a bucket: see the docs.
set -euo pipefail

IMAGE="${1:?usage: smoke_backup.sh <hosted image> [postgres|sqlite]}"
VARIANT="${2:-postgres}"
case "$VARIANT" in
  postgres) FILE=compose.yaml; OVERLAY=backup/compose.postgres.yaml; DBVOL=pgdata ;;
  sqlite) FILE=compose.sqlite.yaml; OVERLAY=backup/compose.sqlite.yaml; DBVOL=data ;;
  *) echo "variant is postgres or sqlite" >&2; exit 2 ;;
esac
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$HERE/deploy/droplet"
PROJECT="semula-backup-smoke-$VARIANT-$$"
ENVFILE="$(mktemp)"
HTTP_PORT="$(python3 "$HERE/scripts/free_port.py" 19080)"
HTTPS_PORT="$(python3 "$HERE/scripts/free_port.py" $((HTTP_PORT + 1)))"
PASSPHRASE="$(openssl rand -hex 16)"
cat >"$ENVFILE" <<ENV
SEMULA_DOMAIN=localhost
SEMULA_IMAGE=$IMAGE
SEMULA_SECRET=$(openssl rand -hex 32)
HTTP_PORT=$HTTP_PORT
HTTPS_PORT=$HTTPS_PORT
POSTGRES_OWNER_PASSWORD=$(openssl rand -hex 16)
SEMULA_DB_PASSWORD=$(openssl rand -hex 16)
SEMULA_BACKUP_PASSPHRASE=$PASSPHRASE
ENV
compose() { docker compose -p "$PROJECT" --env-file "$ENVFILE" -f "$DIR/$FILE" -f "$DIR/$OVERLAY" "$@"; }
cleanup() { compose down -v --remove-orphans >/dev/null 2>&1 || true; rm -f "$ENVFILE"; }
trap cleanup EXIT
fail() { echo "FAIL: $*"; compose logs --no-color --tail 40 2>&1 | tail -80; exit 1; }
admin() { compose run --rm -T admin "$@"; }
base="https://localhost:$HTTPS_PORT"

compose up -d --build --wait --wait-timeout 240 >/dev/null || fail "the stack did not come up"
admin workspace-create acme --name Acme >/dev/null || fail "workspace-create"
secret="$(admin token-create --workspace acme --name drill --scope replay --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
[ -n "$secret" ] || fail "no token"
admin token-list --workspace acme | grep -q drill || fail "the token is not there before the backup"
[ "$(curl -ks -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $secret" "$base/v1/models")" = 200 ] \
  || fail "the token did not work before the backup"

# 1. a backup, as the service makes them
start=$(date +%s)
compose exec -T backup semula-backup backup || fail "the backup failed"
backup_seconds=$(( $(date +%s) - start ))
made="$(compose exec -T backup sh -c 'ls -1 /backups | sort | tail -1' | tr -d '[:space:]')"
case "$made" in semula-*.enc) ;; *) fail "the newest backup is missing or not encrypted: $made" ;; esac
compose exec -T backup semula-backup check --max-hours 1 || fail "check did not pass right after a backup"

# 2. the droplet is lost: everything but the backup volume is gone (a bucket, in real life)
compose stop semula purge caddy backup >/dev/null
if [ "$VARIANT" = postgres ]; then gone="db semula purge caddy migrate backup"; else gone="semula purge caddy migrate backup"; fi
# shellcheck disable=SC2086
compose rm -fsv $gone >/dev/null || fail "could not remove the containers"
docker volume rm "${PROJECT}_${DBVOL}" >/dev/null || fail "could not remove the database volume"

# 3. restore from the dump alone
start=$(date +%s)
if [ "$VARIANT" = postgres ]; then
  compose up -d --wait db >/dev/null || fail "the new database did not start"
  compose run --rm -T --no-deps backup semula-backup restore --file "/backups/${made}" >/dev/null \
    || fail "the restore failed"
else
  compose run --rm -T --no-deps backup semula-backup restore --file "/backups/${made}" --into /data >/dev/null \
    || fail "the restore failed"
fi
restore_seconds=$(( $(date +%s) - start ))
compose up -d --wait --wait-timeout 240 >/dev/null || fail "the stack did not come up on the restored data"

# 4. the data is back, and the app serves it
admin token-list --workspace acme | grep -q drill || fail "the token did not survive the restore"
for _ in $(seq 1 30); do curl -ksf "$base/health" >/dev/null 2>&1 && break; sleep 1; done
[ "$(curl -ks -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $secret" "$base/v1/models")" = 200 ] \
  || fail "the app (its own database role) cannot serve the restored data"

echo "OK ($VARIANT): backup took ${backup_seconds}s, restore took ${restore_seconds}s"
