#!/usr/bin/env bash
# Deploy a released version on this droplet: deploy.sh 0.5.0
#
# The deploy user's SSH key is locked to this script (command= in authorized_keys), so the
# version arrives in SSH_ORIGINAL_COMMAND. Only a plain X.Y.Z is accepted. It checks out that
# tag (the compose files belong to the release), points SEMULA_IMAGE at the release's hosted
# image, starts the stack and waits for /health. If anything fails it puts the previous release
# back, so a bad deploy leaves the site on what was running.
set -euo pipefail

version="${1:-${SSH_ORIGINAL_COMMAND:-}}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "deploy: the version must be a plain X.Y.Z (got a different argument)" >&2
  exit 2
fi

repo="${SEMULA_REPO_DIR:-/opt/semula}"
cd "$repo/deploy/droplet"

if [[ -z "${DEPLOY_REEXEC:-}" ]]; then # the script itself may differ in the new tag: run that one
  previous_ref="$(git -C "$repo" rev-parse HEAD)"
  git fetch --quiet --tags origin
  git checkout --quiet --force "v$version"
  DEPLOY_PREVIOUS_REF="$previous_ref" DEPLOY_REEXEC=1 exec bash "$repo/deploy/droplet/deploy.sh" "$version"
fi

image="ghcr.io/leejianrong/semula:${version}-hosted"
previous_ref="${DEPLOY_PREVIOUS_REF:-}"
previous_image="$(grep -E '^SEMULA_IMAGE=' .env | cut -d= -f2- || true)"

set_image() { # .env keeps one SEMULA_IMAGE line
  grep -vE '^SEMULA_IMAGE=' .env > .env.new || true
  printf 'SEMULA_IMAGE=%s\n' "$1" >> .env.new
  chmod 600 .env.new
  mv .env.new .env
}

healthy() {
  local domain
  domain="$(grep -E '^SEMULA_DOMAIN=' .env | cut -d= -f2-)"
  for _ in $(seq 1 30); do
    curl -fsS --max-time 5 "https://${domain}/health" >/dev/null 2>&1 && return 0
    sleep 4
  done
  return 1
}

up() { docker compose pull --quiet && docker compose up -d --wait --remove-orphans; }

echo "deploy: $image"
set_image "$image"
if up && healthy; then
  docker image prune -f >/dev/null
  echo "deploy: $version is up"
  exit 0
fi

echo "deploy: $version did not come up healthy, putting the previous release back" >&2
docker compose logs --tail 40 semula >&2 || true
if [[ -n "$previous_image" ]]; then
  [[ -n "$previous_ref" ]] && git -C "$repo" checkout --quiet --force "$previous_ref" || true
  set_image "$previous_image"
  up || true
fi
exit 1
