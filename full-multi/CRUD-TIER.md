# CRUD HTTP → MariaDB routing tier (full-multi)

This change adds **two independent CRUD Rollouts**, `crud-a` and `crud-b`,
one per application. The complete request path is:

`Client → Istio Ingress → Front A/B → Backend A/B → Istio CRUD VirtualService
→ CRUD A/B → MariaDB A/B active(BLUE) or preview(GREEN)`.

- The Front continues to forward `x-backend-track` and `x-db-track`.
- The Backend forwards HTTP `/api/books` to the `crud-{a,b}-blue` Service, which serves as the in-mesh routing host. It has
  **no database credentials** and makes no SQL connections. It preserves the
  public JSON `backend` identity by stamping its own BLUE/YELLOW prefix.
- `crud-{a,b}-router` (internal `gateways: [mesh]`) inspects `x-db-track`.
  `green` or `canary` → `crud-{a,b}-green`; `blue`, `stable`, absent, or any
  unrecognized value → `crud-{a,b}-blue`.
- The VirtualService **overwrites** `x-crud-db-target` to `canary` or `stable`.
  The CRUD pod uses that internal header to connect to the matching
  `mariadb-{a,b}-canary` (GREEN preview) or `-stable` (BLUE active) Service.
- **Both CRUD blue and green Services point to the same CRUD pod.** These are
  *logical HTTP routes*, not separate sets of CRUD pods. The databases remain
  two independent native TCP MariaDB instances during blue-green preview.
- The MariaDB blue-green Rollouts, `autoPromotionEnabled: false`, `emptyDir`,
  original `prepare/start/promote` workflow and four HTTP canary Rollouts stay
  unchanged. Two new CRUD Rollouts use rolling-update canary with one pod in
  steady state. This yields **eight Rollouts total**.
- Tracing context propagates from Front → Backend → CRUD. Native MariaDB TCP
  cannot be routed by an HTTP header; it is CRUD that makes the SQL choice.

## Deployment order (critical for Argo CD self-heal)

1. Apply this source patch locally and run checks:

   ```bash
   python3 scripts/full-multi-spec.py verify
   bash -n scripts/build-push-full-multi.sh scripts/full-multi.sh
   (cd full-multi/bookshelf && go test ./...)
   oc kustomize full-multi >/dev/null
   ```

2. **Build and push `ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-crud-v3`
   before committing/pushing the GitOps manifests.** You must have GHCR
   credentials to push, and the package must allow OpenShift image pulls.

   ```bash
   bash scripts/build-push-full-multi.sh
   skopeo inspect --no-creds \
     docker://ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-crud-v3
   ```

3. Commit/push source and manifests together; let `Application/full-multi`
   auto-sync. This is a **coordinated cutover**: until the CRUD Service pods
   are available, any Backend that has switched to `CRUD_API_URL` may return
   502. The sync cannot be completely downtime-free without a staged release.

4. Inspect the CRUD and MariaDB resources:

   ```bash
   oc -n full-multi get rollout crud-a crud-b mariadb-a mariadb-b
   oc -n full-multi get pods -l 'app in (crud-a,crud-b,mariadb-a,mariadb-b)'
   oc -n full-multi get virtualservice crud-a-router crud-b-router
   oc -n full-multi get endpointslice -l kubernetes.io/service-name=crud-a-green
   oc -n full-multi get endpointslice -l kubernetes.io/service-name=mariadb-a-canary
   ```

5. Test both tracks per app without masking HTTP errors with `jq`:

   ```bash
   HOST="$(oc -n full-multi get route full-multi -o jsonpath='{.spec.host}')"
   for app in a b; do
     for db in blue green; do
       echo "--- application=$app database=$db"
       curl -ksS --fail-with-body \
         -H "x-app-target: $app" -H 'x-front-track: stable' \
         -H 'x-backend-track: stable' -H "x-db-track: $db" \
         "https://${HOST}/api/books" | \
         jq '{application,backend,database_track,database_host,books:(.books|length)}'
     done
   done
   ```

Expected JSON `database_track` values remain `stable`/`canary`, regardless of
`blue`/`green` input aliases. GREEN is only accessible while the MariaDB
preview is healthy and backed by a ready EndpointSlice.

**Lab safety:** The MariaDB Rollouts still use `emptyDir`: when BLUE is scaled
down after GREEN promotion, BLUE database contents are permanently discarded.
This architecture is not a safe production database migration design.
