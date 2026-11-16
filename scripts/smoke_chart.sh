#!/usr/bin/env bash
# Install the chart into a NEW namespace on the cluster kubectl points at, prove it, and remove it.
# Proves: it comes up healthy; the API key is enforced; an unknown call is held in record mode, an
# agent answers it, and the app gets the answer; the answer replays and survives deleting the pod,
# a `helm upgrade` and a `helm rollback`; and uninstall leaves the data claim behind (SQLite).
# It refuses to use a namespace that already exists, and only ever deletes the one it made.
# Usage: scripts/smoke_chart.sh [sqlite|postgres]
#   HELM_ARGS="--set image.repository=semula --set image.tag=ci --set image.pullPolicy=Never"
#   NAMESPACE=semula-test (default)   KUBE_CONTEXT=<context> (default: the current one)
# Needs helm, kubectl, curl and python3.
set -euo pipefail

VARIANT="${1:-sqlite}"
NS="${NAMESPACE:-semula-test}"
REL=semula
CHART="$(cd "$(dirname "$0")/.." && pwd)/deploy/helm/semula"
read -r -a EXTRA <<<"${HELM_ARGS:-}"
CTX=()
[ -n "${KUBE_CONTEXT:-}" ] && CTX=(--kube-context "$KUBE_CONTEXT")
case "$VARIANT" in sqlite) VARIANT_ARGS=() ;; postgres) VARIANT_ARGS=(--set postgresql.enabled=true) ;;
  *) echo "variant is sqlite or postgres" >&2; exit 2 ;; esac
k() { kubectl "${CTX[@]}" -n "$NS" "$@"; }
h() { helm "${CTX[@]}" -n "$NS" "$@"; }

kubectl "${CTX[@]}" get ns "$NS" >/dev/null 2>&1 && { echo "namespace $NS already exists: not touching it (set NAMESPACE)" >&2; exit 2; }
PF=""
cleanup() {
  [ -n "$PF" ] && kill "$PF" 2>/dev/null || true
  helm "${CTX[@]}" -n "$NS" uninstall "$REL" >/dev/null 2>&1 || true
  # Wait until it is really gone (a pod takes up to 30 s to stop): the next run refuses a namespace
  # that still exists, and must find this one cleared.
  kubectl "${CTX[@]}" delete ns "$NS" --wait=true --timeout=180s >/dev/null 2>&1 || true
}
trap cleanup EXIT
fail() { echo "FAIL: $*"; k get pods 2>&1 | tail -8; k logs deploy/$REL --tail 30 2>&1 | tail -30; exit 1; }

h upgrade --install "$REL" "$CHART" --create-namespace --wait --timeout 240s "${VARIANT_ARGS[@]}" "${EXTRA[@]}" >/dev/null ||
  fail "helm install did not become ready"
KEY=$(k get secret "$REL-auth" -o jsonpath='{.data.api-key}' | base64 -d)
[ "${#KEY}" -ge 32 ] || fail "no generated API key"

PORT="$(python3 "$(dirname "$0")/free_port.py" 18280)"
forward() {
  [ -n "$PF" ] && kill "$PF" 2>/dev/null || true
  k port-forward "svc/$REL" "$PORT:80" >/dev/null 2>&1 & PF=$!
  for _ in $(seq 1 40); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && return 0; sleep 0.5; done
  fail "port-forward to the service did not answer /health"
}
base="http://127.0.0.1:$PORT"
auth=(-H "Authorization: Bearer $KEY")
forward
curl -sf "$base/health" | grep -q '"status":"ok"' || fail "/health"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$base/v1/models")" = 401 ] || fail "no key was not 401"
[ "$(curl -s -o /dev/null -w '%{http_code}' "${auth[@]}" "$base/v1/models")" = 200 ] || fail "the key was not accepted"

body='{"model":"m","messages":[{"role":"user","content":"chart smoke ping"}]}'
out="$(mktemp)"
curl -s --max-time 90 -X POST "$base/v1/chat/completions" "${auth[@]}" -H 'Content-Type: application/json' -d "$body" >"$out" &
call=$!
pending=""
for _ in $(seq 1 30); do
  pending=$(curl -sf "${auth[@]}" "$base/api/v1/pending?wait=2" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4 || true)
  [ -n "$pending" ] && break
done
[ -n "$pending" ] || fail "the call never parked (mode is $(curl -s "$base/health"))"
curl -sf -X POST "$base/api/v1/pending/$pending/answer" "${auth[@]}" -H 'Content-Type: application/json' \
  -d '{"content":"pong from the chart"}' >/dev/null || fail "answering the parked call"
wait "$call" || fail "the held call did not complete"
grep -q 'pong from the chart' "$out" || fail "the app did not get the answer: $(cat "$out")"
rm -f "$out"

replays() { curl -s --max-time 10 -X POST "$base/v1/chat/completions" "${auth[@]}" -H 'Content-Type: application/json' -d "$body" | grep -q 'pong from the chart'; }
# The bundled database's pod must not be in the app's Service.
only_app_serves() {
  local n; n=$(k get endpoints "$REL" -o jsonpath='{range .subsets[*].addresses[*]}{.targetRef.name}{"\n"}{end}' | grep -vc "^$REL-[a-z0-9]*-[a-z0-9]*$" || true)
  [ "$n" = 0 ] || fail "the Service selects a pod that is not the app: $(k get endpoints "$REL" -o jsonpath='{.subsets[*].addresses[*].targetRef.name}')"
}
ready() { k rollout status "deploy/$REL" --timeout 120s >/dev/null && forward; }
replays || fail "the recorded answer did not replay"
only_app_serves

k delete pod -l app.kubernetes.io/name=semula,app.kubernetes.io/component=server --wait=true >/dev/null
ready || fail "no new pod after deleting the pod"
replays || fail "the recording is gone after the pod was deleted"

h upgrade "$REL" "$CHART" --wait --timeout 240s --set env.SEMULA_LOG_RETENTION_DAYS=7 "${VARIANT_ARGS[@]}" "${EXTRA[@]}" >/dev/null ||
  fail "helm upgrade"
ready || fail "not ready after the upgrade"
k exec "deploy/$REL" -- printenv SEMULA_LOG_RETENTION_DAYS | grep -qx 7 || fail "the upgrade's value did not reach the pod"
[ "$(k get secret "$REL-auth" -o jsonpath='{.data.api-key}' | base64 -d)" = "$KEY" ] || fail "an upgrade changed the API key"
replays || fail "the recording is gone after helm upgrade"

h rollback "$REL" 1 --wait --timeout 240s >/dev/null || fail "helm rollback"
ready || fail "not ready after the rollback"
replays || fail "the recording is gone after helm rollback"

h uninstall "$REL" --wait >/dev/null || fail "helm uninstall"
if [ "$VARIANT" = sqlite ]; then
  k get pvc "$REL" >/dev/null 2>&1 || fail "uninstall deleted the data claim (it must be kept)"
fi
echo "ok ($VARIANT): chart installs, enforces the key, holds then answers a call, replays it across a pod restart, an upgrade and a rollback, and uninstall keeps the data"
