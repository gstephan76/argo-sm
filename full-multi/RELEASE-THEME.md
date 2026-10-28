# ConfigMap-driven release identity and styling

The `bookshelf-release-theme` ConfigMap contains `theme.json` with two
independent palettes, `blue` and `yellow`. Each supports `label`, `subtitle`,
`accent`, `surface`, and `ink`. Change these values in
`full-multi/configmap-release-theme.yaml` and push through GitOps to update
both HTTP tiers **without a new image**. Kubelet projects the updated file;
the Go application reads it for each request (do not use `subPath`). Only
strict #RRGGBB color strings are accepted; malformed data uses defaults.

The actual Frontend/Backend release identity is stamped by each serving pod,
not inferred from request headers. BLUE/YELLOW describes executable revision,
not a permanent stable/canary Service ownership. After promotion, a YELLOW
revision can be stable. `/api/books` includes frontend/backend release, pod,
label, accent, plus the existing database response. The web page presents
two illustrated, color-coded cards and the database selection. `/info` and
Argo Rollouts analysis contracts remain unchanged.

## Deploy BLUE baseline, then YELLOW candidate

From the repository root:

```
python3 update_full_multi_release_theme_v2.py --phase install --check
python3 update_full_multi_release_theme_v2.py --phase install --apply
python3 scripts/full-multi-spec.py verify
oc kustomize full-multi >/dev/null
(cd full-multi/bookshelf && go test ./...)
bash scripts/build-push-full-multi.sh
skopeo inspect --no-creds docker://ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-release-theme-v2
```

Build/push the new immutable image **before** pushing the GitOps manifests.
Commit/push the installer-applied changes. After Argo CD is synced and the four
HTTP Rollouts pass their analysis/pause, promote Frontend and Backend only.
Once all four are Healthy and all old pods drain, run the installer with
`--phase candidate --check` and `--apply`, commit/push only the four HTTP
Rollout manifests, and leave them paused at 50%. MariaDB is untouched.

## Routing verification

```
HOST="$(oc -n full-multi get route full-multi -o jsonpath='{.spec.host}')"
for ft in stable canary; do
  for bt in stable canary; do
    echo "FRONT=$ft / BACKEND=$bt"
    curl -kfsS -H 'x-app-target: a' -H "x-front-track: $ft" \
      -H "x-backend-track: $bt" -H 'x-db-track: blue' \
      "https://${HOST}/api/books" | \
      jq '{frontend_release,frontend_pod,frontend_label,frontend_accent,backend_release,backend_pod,backend_label,backend_accent,database_host}'
  done
done
```

Browser homepage: `https://${HOST}/` (weighted by Istio when no track headers
are supplied). A browser address bar cannot set custom headers; use curl or a
header-extension to force combinations. The existing 17 Services, Istio routing,
tracing context, CRUD and MariaDB images remain unchanged.
