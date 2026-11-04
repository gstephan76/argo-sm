#!/usr/bin/env bash
set -Eeuo pipefail
# Existing credentials are never rotated implicitly. MariaDB's internal passwords
# would NOT be updated by a Kubernetes Secret overwrite.
oc apply -f "$(dirname "$0")/../full-multi/namespace.yaml"
present=0
for app in a b; do
  if oc -n full-multi get secret "db-credentials-$app" >/dev/null 2>&1; then
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
: "${FULL_MULTI_DB_PASSWORD:?FULL_MULTI_DB_PASSWORD is required for first provision}"
: "${FULL_MULTI_DB_ROOT_PASSWORD:?FULL_MULTI_DB_ROOT_PASSWORD is required for first provision}"
# No credentials in shell arguments, Git, or on-disk manifests.
python3 - <<'PY' | oc apply -f -
import base64,json,os
for app in 'ab':
    data={'password':os.environ['FULL_MULTI_DB_PASSWORD'],
          'root-password':os.environ['FULL_MULTI_DB_ROOT_PASSWORD']}
    obj={'apiVersion':'v1','kind':'Secret','metadata':{'namespace':'full-multi','name':f'db-credentials-{app}'},
         'type':'Opaque','data':{k:base64.b64encode(v.encode()).decode() for k,v in data.items()}}
    print(json.dumps(obj)+'\n---')
PY
