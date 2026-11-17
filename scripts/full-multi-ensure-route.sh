#!/usr/bin/env bash
# Idempotently create the ambient Route and wait for router admission.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $# == 1 && "$1" == --apply ]] || { echo 'Usage: bash scripts/full-multi-ensure-route.sh --apply' >&2; exit 2; }
oc apply -f "$ROOT/full-multi/route.yaml"
for (( attempt=0; attempt<36; attempt++ )); do
  route="$(oc -n multi-ambient get route multi-ambient -o json)"
  if jq -e '.spec.host != null and any(.status.ingress[]?.conditions[]?; .type == "Admitted" and .status == "True")' <<<"$route" >/dev/null; then
    echo 'PASS Route/multi-ambient admitted'
    exit 0
  fi
  sleep 5
done
echo 'ERROR: Route/multi-ambient was not admitted within 180 seconds' >&2
exit 1
