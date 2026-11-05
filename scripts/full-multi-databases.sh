#!/usr/bin/env bash
# Explicit staging of four new persistent MariaDB instances before GitOps cutover.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS=full-multi
[[ "${1:-}" == "--apply" && $# -eq 1 ]] || {
  echo 'Usage: bash scripts/full-multi-databases.sh --apply' >&2; exit 2;
}
command -v oc >/dev/null || { echo 'oc is required' >&2; exit 1; }
oc get ns "$NS" >/dev/null
for app in a b; do
  oc -n "$NS" get secret "db-credentials-$app" >/dev/null
done
# Validate dynamic storage provisioner availability before creating PVCs.
oc get storageclass -o json | python3 -c 'import json,sys; sc=json.load(sys.stdin); sys.exit(0 if any((x.get("metadata",{}).get("annotations",{}).get("storageclass.kubernetes.io/is-default-class") == "true" or x.get("metadata",{}).get("annotations",{}).get("storageclass.beta.kubernetes.io/is-default-class") == "true") for x in sc.get("items",[])) else 1)' || {
  echo 'ERROR: no default StorageClass; inspect oc get storageclass' >&2; exit 1;
}
for app in a b; do
  for track in blue green; do
    name="mariadb-$app-$track"
    oc apply -f "$ROOT/full-multi/service-$name.yaml"
    oc apply -f "$ROOT/full-multi/statefulset-$name.yaml"
    oc -n "$NS" rollout status "statefulset/$name" --timeout=600s
    oc -n "$NS" get pvc "mariadb-data-$name-0" -o jsonpath='{.status.phase}' | grep -qx Bound || {
      echo "ERROR: PVC for $name not Bound" >&2; exit 1;
    }
  done
done
echo 'PASS four independent MariaDB StatefulSets and their persistent PVCs are ready.'
echo 'NEXT: verify images/datasets, then deliberately authorize the GitOps cutover.'
