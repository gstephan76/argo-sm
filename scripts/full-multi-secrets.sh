#!/usr/bin/env bash
set -Eeuo pipefail
: "${FULL_MULTI_DB_PASSWORD:?FULL_MULTI_DB_PASSWORD is required}"
: "${FULL_MULTI_DB_ROOT_PASSWORD:?FULL_MULTI_DB_ROOT_PASSWORD is required}"
# No credentials in shell arguments, Git, or on-disk manifests.
oc apply -f "$(dirname "$0")/../full-multi/namespace.yaml"
python3 - <<'PY' | oc apply -f -
import base64,json,os
for app in 'ab':
    data={'password':os.environ['FULL_MULTI_DB_PASSWORD'],
          'root-password':os.environ['FULL_MULTI_DB_ROOT_PASSWORD']}
    obj={'apiVersion':'v1','kind':'Secret','metadata':{'namespace':'full-multi','name':f'db-credentials-{app}'},
         'type':'Opaque','data':{k:base64.b64encode(v.encode()).decode() for k,v in data.items()}}
    print(json.dumps(obj)+'\n---')
PY
