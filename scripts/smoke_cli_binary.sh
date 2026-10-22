#!/usr/bin/env bash
# Prove a built CLI binary works the way a user meets it: it starts, reports its version, talks to
# a running semula over HTTP, and says plainly what it leaves out.
# Usage: scripts/smoke_cli_binary.sh <binary> <server-url> [expected-version]
set -euo pipefail

BIN="${1:?usage: smoke_cli_binary.sh <binary> <server-url> [version]}"
URL="${2:?usage: smoke_cli_binary.sh <binary> <server-url> [version]}"
WANT="${3:-}"

fail() { echo "FAIL: $*"; exit 1; }

version=$("$BIN" --version)
echo "$version"
[ -z "$WANT" ] || [ "$version" = "semula $WANT" ] || fail "version is '$version', wanted 'semula $WANT'"

"$BIN" --url "$URL" mode >/dev/null || fail "mode"
"$BIN" --url "$URL" pending | grep -q "no pending requests" || fail "pending"
"$BIN" --url "$URL" pending --wait 1 --json | grep -qx '\[\]' || fail "pending --wait"
"$BIN" --url "$URL" project create smoke-binary >/dev/null || fail "project create"
"$BIN" --url "$URL" entry add smoke-binary --prompt "ping" --text "pong" >/dev/null || fail "entry add"
"$BIN" --url "$URL" entry list smoke-binary --json | grep -q '"total": *1' || fail "entry list"
"$BIN" --url "$URL" test-match --project smoke-binary --prompt "ping" | grep -qi "serve\|match" \
  || fail "test-match"
"$BIN" --url "$URL" log --stats >/dev/null || fail "log --stats"

for cmd in serve mcp; do
  set +e
  out=$("$BIN" "$cmd" 2>&1)
  code=$?
  set -e
  [ "$code" = 2 ] && echo "$out" | grep -q "not in this build" \
    || fail "'$cmd' should exit 2 and say it is not in this build (got $code: $out)"
done
echo "ok: $BIN ($version) talked to $URL"
