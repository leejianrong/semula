#!/usr/bin/env bash
# Lint and render the Helm chart for every value set in deploy/helm/semula/ci (all must pass) and
# ci-fail (all must be refused, with a reason). Needs helm; runs kubeconform too when it is installed.
# Usage: scripts/check_chart.sh        (make chart-check)
set -euo pipefail
CHART="$(cd "$(dirname "$0")/.." && pwd)/deploy/helm/semula"
command -v helm >/dev/null || { echo "helm is not installed" >&2; exit 2; }

helm lint --strict "$CHART" >/dev/null
for f in "$CHART"/ci/*.yaml; do
  name=$(basename "$f")
  helm lint --strict "$CHART" -f "$f" >/dev/null || { echo "FAIL: lint with $name"; exit 1; }
  out=$(helm template t "$CHART" -f "$f") || { echo "FAIL: template with $name"; exit 1; }
  if command -v kubeconform >/dev/null; then
    echo "$out" | kubeconform -strict -summary -ignore-missing-schemas >/dev/null || { echo "FAIL: kubeconform with $name"; exit 1; }
  fi
  echo "ok: $name"
done
for f in "$CHART"/ci-fail/*.yaml; do
  name=$(basename "$f")
  if err=$(helm template t "$CHART" -f "$f" 2>&1); then echo "FAIL: $name was accepted"; exit 1; fi
  [ -n "$err" ] || { echo "FAIL: $name was refused with no reason"; exit 1; }
  echo "ok (refused): $name"
done

# What the values promise shows up in what is rendered.
render() { helm template t "$CHART" "$@"; }
render | grep -q 'type: Recreate' || { echo "FAIL: SQLite must use the Recreate strategy"; exit 1; }
render | grep -q 'image: "ghcr.io/leejianrong/semula:[0-9.]*"$' || { echo "FAIL: default image has no plain tag"; exit 1; }
render -f "$CHART/ci/postgres-bundled.yaml" | grep -q 'semula:[0-9.]*-hosted' || { echo "FAIL: Postgres needs the -hosted image"; exit 1; }
render -f "$CHART/ci/postgres-bundled.yaml" | grep -q 'type: RollingUpdate' || { echo "FAIL: Postgres may roll"; exit 1; }
render --set image.repository=ghcr.io/me/semula --set image.tag=1.2.3 | grep -q 'ghcr.io/me/semula:1.2.3' || { echo "FAIL: a fork's image is ignored"; exit 1; }
render -f "$CHART/ci/replay-ephemeral.yaml" | grep -q SEMULA_API_KEY && { echo "FAIL: auth.disabled still sets a key"; exit 1; }
render | grep -q 'readOnlyRootFilesystem: true' || { echo "FAIL: the root filesystem is writable"; exit 1; }
echo "ok: chart"
