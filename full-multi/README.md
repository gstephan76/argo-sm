# full-multi: six Rollouts, two independent Bookshelf A/B stacks

`full-multi` is a NEW isolated namespace and GitOps Application. It does not edit
`full-single-service` or the original `bookshelf` namespace. One shared
OpenShift Route and Istio ingress gateway selects A/B; Front and Backend are
independent HTTP canaries; MariaDB is a separate blue-green Rollout per app.

```
             Route/full-multi   [x-app-target: a|b]
                      |
             VirtualService/front-router
                /                 \
       FRONT-A Rollout       FRONT-B Rollout
       stable/canary         stable/canary
                |                 |
          [x-backend-track: stable|canary] (forwarded by Front)
                |                 |
       BACKEND-A Rollout     BACKEND-B Rollout
       stable/canary         stable/canary
                |                 |
      [x-db-track: stable|canary] (used by Backend, NOT by MariaDB TCP)
                |                 |
       MariaDB-A Rollout     MariaDB-B Rollout
       blue-green           blue-green
       stable/preview        stable/preview
```

Exactly six `Rollout` objects: `front-a`, `backend-a`, `mariadb-a`, `front-b`,
`backend-b`, `mariadb-b`. Each has `*-stable` and `*-canary` Services (12 total),
plus two virtual backend DNS anchor Services `backend-a` and `backend-b`.
There are four HTTP canary strategies and two MariaDB blue-green strategies.
The same custom image runs all four Front/Backend Rollouts; the DB Rollouts use
`quay.io/sclorg/mariadb-1011-c9s:latest`.

## HTTP headers and route precedence

| Header | Where interpreted | Behavior |
|--|--|--|
| `x-app-target: b` | gateway | Select B; missing/other selects A |
| `x-front-track: stable|canary` | gateway | Force Front track; absent uses Istio weighted route |
| `x-backend-track: stable|canary` | Backend VirtualService | Force Backend track; absent uses independent weighted route |
| `x-db-track: stable|canary` | Backend application | Chooses MariaDB A/B stable or preview connection per request |

The frontend **forwards** `x-backend-track` and `x-db-track` when requesting the
Backend. Istio does not propagate custom HTTP headers through a new app HTTP
request automatically. The Backend chooses its MariaDB host using a strict
stable/canary whitelist; MariaDB receives SQL over TCP and never receives HTTP
headers. Both apps use independent MariaDB Services and `bookdb` databases.

## State safety: DEMO ONLY

MariaDB `emptyDir` data is **ephemeral** and different for every DB ReplicaSet.
It is initialized with three demo books by the Backend on the first successful
request. Any updates are discarded upon pod recreation or rollout promotion.
This intentionally avoids sharing one writable database PVC across stable and
preview database pods. Do not use this example as a persistent or production DB
solution; use StatefulSets, durable independent datasets, replication and an
explicit migration/cutover plan for durable use cases.

## Prerequisites

OpenShift 4.x with Argo CD, Argo Rollouts CLI/plugin, OSSM/Istio injection, a
working `otel-tracing` Istio extension provider for tracing, Podman or Docker,
and a Git checkout of `gstephan76/argo-sm` on clean `main` with push rights.
`full-multi/bookshelf/` is a minimal adapted header-aware Bookshelf app; it is
NOT byte-for-byte the existing `quay.io/gstephan/{frontend,backend}:latest`
applications, because those prebuilt images have not been verified to forward
custom request headers or choose MariaDB per HTTP request.

## Build and publish before committing manifests

The new image must be pullable by OpenShift before GitOps deploy:

```bash
export GHCR_TOKEN=...    # Load privately from your password manager; never commit
bash scripts/build-push-full-multi.sh
unset GHCR_TOKEN
skopeo inspect --no-creds docker://ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-v1
```

Set the GHCR package **public**, or configure private imagePullSecrets in the
Rollouts yourself. The image is intentionally version-tagged; use immutable
image digests for production.

## Set private secrets, then GitOps deploy

First create namespace and two DB Secrets from environment variables. Never
commit passwords into YAML:

```bash
export FULL_MULTI_DB_PASSWORD='...'
export FULL_MULTI_DB_ROOT_PASSWORD='...'
bash scripts/full-multi-secrets.sh
unset FULL_MULTI_DB_PASSWORD FULL_MULTI_DB_ROOT_PASSWORD

# Commit and push the new source files (commands provided with the artifact).
INSTANCE=a bash scripts/full-multi.sh validate
INSTANCE=a bash scripts/full-multi.sh deploy
```

## Demo workflow

```bash
INSTANCE=a bash scripts/full-multi.sh prepare
INSTANCE=a bash scripts/full-multi.sh start
INSTANCE=a bash scripts/full-multi.sh promote

INSTANCE=b bash scripts/full-multi.sh prepare
INSTANCE=b bash scripts/full-multi.sh start
INSTANCE=b bash scripts/full-multi.sh promote
```

`prepare` restores BLUE and automatically attempts to clean **completed**
Rollout-owned history after all three tiers are healthy. Database history is
never removed while its prior ReplicaSet still has live replicas. `start`
creates new YELLOW candidate revisions and waits for the Front/Backend final
manual canary pause and the MariaDB preview pause. `promote` approves all three.

**Caution:** MariaDB preview availability/phase needs confirmation against the
installed version of Argo Rollouts; the scripts are not yet live-cluster tested.
Do not run mutating commands concurrently; the scripts expect clean `main`
whose HEAD equals `origin/main`.

## Verify header-directed requests

```bash
URL="https://$(oc get route full-multi -n full-multi -o jsonpath='{.spec.host}')/"
curl -kfsS -H 'x-app-target: a' -H 'x-front-track: stable' \
 -H 'x-backend-track: stable' -H 'x-db-track: stable' "$URL"
curl -kfsS -H 'x-app-target: b' -H 'x-front-track: canary' \
 -H 'x-backend-track: canary' -H 'x-db-track: canary' "$URL"
# Exact backend/database routing evidence is in /api/books metadata:
curl -kfsS -H 'x-app-target: b' -H 'x-db-track: canary' "${URL}api/books"
```

Track overrides require the corresponding canary/preview pods to exist; if not,
HTTP 503 is expected. The stable track is the default. The analysis templates
perform 15 direct-canary /info and database-backed /api/books checks per gate.

## Observability

`Telemetry/full-multi-tracing` uses the existing `otel-tracing` extension
provider and samples 100% of this demo namespace for Tempo. Review collector
endpoint, Tempo tenant and RBAC separately. Sidecars can trace HTTP hops; TCP
MariaDB traffic yields L4 telemetry, not SQL-specific trace spans without
application-level DB instrumentation.
