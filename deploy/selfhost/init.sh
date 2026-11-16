#!/usr/bin/env bash
# Write .env with fresh secrets. Safe to run again: it never replaces a value that is already set.
#   ./init.sh semula.example.com
set -euo pipefail
cd "$(dirname "$0")"
domain="${1:-}"
[ -f .env ] || cp .env.example .env
chmod 600 .env
set_default() { # set KEY=value only when KEY is empty or missing
  local key="$1" value="$2"
  if grep -q "^$key=." .env; then return; fi
  if grep -q "^$key=" .env; then
    sed -i.bak "s|^$key=.*|$key=$value|" .env && rm -f .env.bak
  else
    echo "$key=$value" >>.env
  fi
}
secret() { openssl rand -hex 32; }
set_default SEMULA_API_KEY "$(secret)"
set_default POSTGRES_PASSWORD "$(secret)"
if [ -n "$domain" ]; then
  sed -i.bak "s|^SEMULA_DOMAIN=.*|SEMULA_DOMAIN=$domain|" .env && rm -f .env.bak
fi
echo "wrote $(pwd)/.env (mode 600)"
echo "domain:  $(grep '^SEMULA_DOMAIN=' .env | cut -d= -f2-)"
echo "next:    docker compose up -d --wait"
echo "key:     in .env as SEMULA_API_KEY (the CLI and agents read it from the environment)"
