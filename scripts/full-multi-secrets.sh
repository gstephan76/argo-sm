#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
oc_read(){ python3 "$ROOT/scripts/full_multi_oc.py" "$@"; }
generate_demo=0
if [[ "${1:-}" == --generate-demo && $# == 1 ]]; then
  generate_demo=1
elif (( $# != 0 )); then
  echo 'Usage: bash scripts/full-multi-secrets.sh [--generate-demo]' >&2
  exit 2
fi
# Existing credentials are never rotated implicitly. MariaDB's internal passwords
# would NOT be updated by a Kubernetes Secret overwrite.
oc apply -f "$ROOT/full-multi/namespace.yaml"
present=0
missing_apps=()
for app in a b; do
  # --ignore-not-found distinguishes an absent Secret from an API/RBAC error.
  # Only request the resource name; never print or process existing contents.
  name="$(oc_read get secret "db-credentials-$app" -n multi-ambient --ignore-not-found -o name)"
  if [[ -n "$name" ]]; then
    present=$((present + 1))
  else
    missing_apps+=("$app")
  fi
done
if (( present == 2 )); then
  echo 'PASS: db-credentials-a and db-credentials-b already exist; left unchanged (no rotation)'
  exit 0
fi
if (( present == 1 && ! generate_demo )); then
  echo 'ERROR: Only one database Secret exists; refusing partial overwrite. Inspect both Secrets.' >&2
  exit 1
fi
# A missing Secret can be an interrupted first provision OR deleted live
# credentials. Never generate replacement credentials for existing data.
resource_kinds=statefulsets,deployments,pods,replicasets,persistentvolumeclaims,jobs,cronjobs
rollouts_crd="$(oc_read get crd rollouts.argoproj.io --ignore-not-found -o name)"
if [[ -n "$rollouts_crd" ]]; then
  resource_kinds+=,rollouts.argoproj.io
fi
oc_read get "$resource_kinds" \
  -n multi-ambient -o json | python3 -c '
import json,re,sys
items=json.load(sys.stdin).get("items", [])
for app in sys.argv[1:]:
    secret=f"db-credentials-{app}"
    def references(value):
        if isinstance(value,dict): return any(references(v) for v in value.values())
        if isinstance(value,list): return any(references(v) for v in value)
        return value == secret
    for item in items:
        name=item.get("metadata",{}).get("name", "")
        if (re.match(rf"(?:mariadb-data-)?(?:mariadb|front|backend|crud)-{app}(?:-|$)",name)
                or references(item.get("spec",{}))):
            sys.exit(f"ERROR: app {app.upper()} has storage or workloads but its database Secret is missing; restore its original credentials before retrying")
' "${missing_apps[@]}"
if (( ! generate_demo )); then
  : "${FULL_MULTI_DB_PASSWORD:?FULL_MULTI_DB_PASSWORD is required for first provision}"
  : "${FULL_MULTI_DB_ROOT_PASSWORD:?FULL_MULTI_DB_ROOT_PASSWORD is required for first provision}"
fi
# No credentials in shell arguments, Git, or on-disk manifests.
# Create, never apply: a concurrent creator must cause a safe failure rather
# than allowing us to overwrite credentials after the existence check.
FULL_MULTI_GENERATE_DEMO="$generate_demo" FULL_MULTI_MISSING_APPS="${missing_apps[*]}" python3 - <<'PY' | oc create -f -
import base64,json,os,secrets
if os.environ['FULL_MULTI_GENERATE_DEMO'] == '1':
    password, root_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
else:
    password, root_password = os.environ['FULL_MULTI_DB_PASSWORD'], os.environ['FULL_MULTI_DB_ROOT_PASSWORD']
for app in os.environ['FULL_MULTI_MISSING_APPS'].split():
    data={'password':password, 'root-password':root_password}
    obj={'apiVersion':'v1','kind':'Secret','metadata':{'namespace':'multi-ambient','name':f'db-credentials-{app}'},
         'type':'Opaque','data':{k:base64.b64encode(v.encode()).decode() for k,v in data.items()}}
    print(json.dumps(obj)+'\n---')
PY
