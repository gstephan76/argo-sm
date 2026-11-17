#!/usr/bin/env bash
set -Eeuo pipefail
generate_demo=0
if [[ "${1:-}" == --generate-demo && $# == 1 ]]; then
  generate_demo=1
elif (( $# != 0 )); then
  echo 'Usage: bash scripts/full-multi-secrets.sh [--generate-demo]' >&2
  exit 2
fi
# Existing credentials are never rotated implicitly. MariaDB's internal passwords
# would NOT be updated by a Kubernetes Secret overwrite.
oc apply -f "$(dirname "$0")/../full-multi/namespace.yaml"
present=0
for app in a b; do
  # --ignore-not-found distinguishes an absent Secret from an API/RBAC error.
  # Only request the resource name; never print or process existing contents.
  name="$(oc -n multi-ambient get secret "db-credentials-$app" --ignore-not-found -o name)"
  if [[ -n "$name" ]]; then
    present=$((present + 1))
  fi
done
if (( present == 2 )); then
  echo 'PASS: db-credentials-a and db-credentials-b already exist; left unchanged (no rotation)'
  exit 0
fi
if (( present == 1 )); then
  echo 'ERROR: Only one database Secret exists; refusing partial overwrite. Inspect both Secrets.' >&2
  exit 1
fi
if (( ! generate_demo )); then
  : "${FULL_MULTI_DB_PASSWORD:?FULL_MULTI_DB_PASSWORD is required for first provision}"
  : "${FULL_MULTI_DB_ROOT_PASSWORD:?FULL_MULTI_DB_ROOT_PASSWORD is required for first provision}"
fi
# No credentials in shell arguments, Git, or on-disk manifests.
# Create, never apply: a concurrent creator must cause a safe failure rather
# than allowing us to overwrite credentials after the existence check.
FULL_MULTI_GENERATE_DEMO="$generate_demo" python3 - <<'PY' | oc create -f -
import base64,json,os,secrets
if os.environ['FULL_MULTI_GENERATE_DEMO'] == '1':
    password, root_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
else:
    password, root_password = os.environ['FULL_MULTI_DB_PASSWORD'], os.environ['FULL_MULTI_DB_ROOT_PASSWORD']
for app in 'ab':
    data={'password':password, 'root-password':root_password}
    obj={'apiVersion':'v1','kind':'Secret','metadata':{'namespace':'multi-ambient','name':f'db-credentials-{app}'},
         'type':'Opaque','data':{k:base64.b64encode(v.encode()).decode() for k,v in data.items()}}
    print(json.dumps(obj)+'\n---')
PY
