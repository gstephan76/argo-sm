# full-single-service — Application A/B BLUE → YELLOW Istio canaries

This is an Argo CD + Argo Rollouts + OpenShift Service Mesh demo in namespace
`full-single-service`, **not a Knative Service**. Both Rollouts reference
the **same container image**, defined in `greeter-image.txt`. The greeter is
built from `greeter/` and implements the original plain-text greeter format.

The response identifier is loaded at process startup from `GREETER_INSTANCE_ID`,
supplied by `configmap-a-identity.yaml` or `configmap-b-identity.yaml`. The
BLUE/YELLOW prefix remains controlled by each Rollout's `MESSAGE_PREFIX`, and
each pod retains its own incrementing request counter. The image contains
no A/B-specific identity.

## Responses

`GET /` responds with one plain-text line like:

```text
Application A - BLUE greeter => 'greeter-a-8f1c4e' : 1
Application A - BLUE greeter => 'greeter-a-8f1c4e' : 2
Application B - YELLOW greeter => 'greeter-b-3d7a92' : 1
```

The identifier comes from the app-specific ConfigMap and stays constant across
pod restarts. The `Application X - COLOR` prefix comes from `MESSAGE_PREFIX`.
The counter is maintained per pod; count values can vary when traffic is routed
between stable and candidate versions. Canary AnalysisJobs also increment it.

Readiness and liveness probes use TCP on port 8080, avoiding HTTP health-check
requests being included in traffic counts. The greeter does not make downstream
calls, so Kiali will mainly show traffic generator → Istio ingress → A/B pods.
During active canary analysis, the short-lived AnalysisJobs make HTTP requests
as well; choose a later Kiali time window to isolate generator traffic.

## Build and publish the shared image (before syncing the Rollouts)

From repository root, with podman or docker installed and authenticated to GHCR:

```bash
# Optional: export GHCR_TOKEN from your private environment (never commit it).
# This script runs registry login only if GHCR_TOKEN is supplied.
bash scripts/build-push-full-single-service-greeter.sh
```

The published package must be public for these Rollouts to pull it without
an imagePullSecret. Alternatively, configure imagePullSecrets in both
Rollouts before deploying. **Build and push before committing the manifests**:
Argo CD automatically synchronizes `main` and would otherwise reference
an image that does not exist. Use a new immutable image tag for any future
source change. `greeter-image.txt` is the image reference verified for both
Rollouts by `scripts/full-single-service-spec.py`.

You can edit the two identity ConfigMaps without building a new image.
Environment variables are captured when a pod starts, so a ConfigMap edit
requires replacing the pod (normally by a new GitOps Rollout revision)
before the response identifier changes. During a BLUE/YELLOW rollout,
stable and candidate pods of the same application use the same identity.

## One desired replica per application

Both Rollouts declare **`replicas: 1`**. At steady state there is one A pod and
one B pod. During a zero-downtime, weighted canary change (`10/25/50/75/100%`),
`maxSurge: 1` temporarily allows **one additional pod in the updating Rollout**
so stable BLUE and candidate YELLOW coexist. After final promotion the old
ReplicaSet scales down to zero (typically after a short delay).

## Resources

| Resource | Application A | Application B |
|---|---|---|
| Rollout | `full-single-service-a` | `full-single-service-b` |
| YAML | `rollout-a.yaml` | `rollout-b.yaml` |
| OpenShift Route | **One shared Route:** `full-single-service` | Access via the same Route and header |
| Stable Service | `full-single-service-a-stable` | `full-single-service-b-stable` |
| Canary Service | `full-single-service-a-canary` | `full-single-service-b-canary` |
| AnalysisTemplate | `full-single-service-a-smoke-test` | `full-single-service-b-smoke-test` |
| VirtualService route | `primary` | `secondary` |

The Argo Rollouts controller owns the Istio traffic weights and dynamic stable/
canary Service selectors. Argo CD must ignore those controller-owned values.
There is no `full-single-service-stable` Service anymore.

## Validation and demo workflow

```bash
bash scripts/full-single-service.sh validate

INSTANCE=a bash scripts/full-single-service.sh prepare
INSTANCE=a bash scripts/full-single-service.sh start
INSTANCE=a bash scripts/full-single-service.sh promote

INSTANCE=b bash scripts/full-single-service.sh prepare
INSTANCE=b bash scripts/full-single-service.sh start
INSTANCE=b bash scripts/full-single-service.sh promote
```

Run only one mutating operation at a time and check its exit code. `prepare`
commits a canonical BLUE marker, `start` commits a unique YELLOW candidate and
waits for the final pause, and `promote` approves YELLOW after five successful
candidate-Service analysis checks. These scripts push desired state to `main`;
auto-sync can change the cluster immediately.

To see the single-line responses and counters through the shared Route:

```bash
INSTANCE=a SHOW_RESPONSES=1 REQUESTS=40 bash scripts/full-single-service.sh traffic
INSTANCE=b SHOW_RESPONSES=1 REQUESTS=40 bash scripts/full-single-service.sh traffic
```

## Automatic history reset during prepare

`INSTANCE=a|b ... prepare` restores the selected application's BLUE baseline,
waits for all five analysis gates when a rollout occurs, and promotes normally
at the final pause. It then **deletes completed historical AnalysisRuns and
old, scaled-down ReplicaSets owned by the selected Rollout**. It does not
remove the healthy BLUE ReplicaSet or touch the other application's history.
The cleanup waits for running analyses and old replicas to finish and refuses
to delete them while active. This is a destructive default: export important
analysis results before running `prepare`. Revision numbers are not reset.

## Watch VirtualService weights and Rollouts

Open separate terminals while running `start` or `promote`. Both applications
share the public Route and Istio VirtualService, but have **independent**
stable/canary weights. The named Istio HTTP route `secondary` is Application B;
`primary` is Application A. In each route, destination 0 is the stable Service
and destination 1 is the canary Service.

**Terminal 1 — watch live Istio stable/canary weights (every 2 seconds):**

```bash
# Columns: HTTP route | stable weight (%) | canary weight (%)
watch -n 2 -d -- 'oc get virtualservice full-single-service-router -n full-single-service -o json | jq -r ".spec.http[] | [.name, .route[0].weight, .route[1].weight] | @tsv"'
```

**Terminal 2 — watch both Rollouts (desired/current/updated/available replicas):**

```bash
watch -n 2 -d -- 'oc get rollouts.argoproj.io -n full-single-service'
```

**Optional terminals — detailed steps, pauses, and AnalysisRun results:**

```bash
# Application A (run in its own terminal)
oc argo rollouts get rollout full-single-service-a -n full-single-service --watch

# Application B (run in a separate terminal)
oc argo rollouts get rollout full-single-service-b -n full-single-service --watch
```

Press Ctrl+C to stop a watch. At the final manual pause (step 10/11), canary
traffic can be 100% while both the old stable and new candidate pods are still
running. After promotion, the candidate becomes stable and the old ReplicaSet
scales down; the corresponding Istio route normally returns to 100% stable /
0% canary. These weights are managed by Argo Rollouts, not by the public Route.

### Verify ingress traffic in Prometheus (Kiali attribution caveat)

After a promotion, Argo Rollouts can leave the stable and canary Kubernetes
Services selecting the same promoted pod. The Istio gateway routes using the
VirtualService weights (normally 100% stable / 0% canary after promotion), but
destination-side Istio telemetry may attribute requests to the canary Service
because both Services resolve to the same pod and port. As a result, Kiali can
show apparent canary traffic even when the gateway sends none.

For the **gateway routing decision**, open Prometheus and run:

```promql
sum by (destination_service_name) (
  rate(
    istio_requests_total{
      reporter="source",
      source_workload="full-single-service-ingressgateway",
      destination_workload_namespace="full-single-service",
      destination_service_name=~"full-single-service-(a|b)-(stable|canary)"
    }[2m]
  )
)
```

For an application that has finished promotion, expect a nonzero request rate
for its `*-stable` destination while requests are being generated, and zero (or
no series) for its `*-canary` destination. Verify both applications independently.
These are requests per second, not percentage weights. If Kiali still shows a
green canary edge, compare the Istio `source` versus `destination` reporters
before treating it as a routing failure. Direct requests to canary Services
(for example, analysis probes) are separate from ingress routing.

## One Route with header-based A/B selection

Exactly **one** public OpenShift `Route` exists: `full-single-service`.
Both Rollouts share its gateway and `VirtualService` but retain separate stable
and canary Service pairs and independent Istio weights. The old public Routes
`full-single-service-a` and `full-single-service-b` are superseded by this neutral
Route. Argo CD should create `Route/full-single-service` and prune the old names
after the change is synchronized. The generated hostname will change; retrieve
it from OpenShift rather than hardcoding it.

The Route resource and public hostname are **application-neutral**. Neither
`Route/full-single-service` nor its service target selects A/B; the Istio
`VirtualService/full-single-service-router` uses the header to select the backend.

The HTTP header **`x-app-target: b`** sends requests to Application B.
`x-app-target: a` sends requests to Application A. Without the header,
Istio defaults to A. Other values also default to A (only lowercase `b`
selects B). Header-based selection is separate from the BLUE/YELLOW weights
controlled by Argo Rollouts.

Retrieve the single public endpoint and call either application:

```bash
NS=full-single-service
ENTRY_HOST=$(oc get route full-single-service -n "$NS" -o jsonpath='{.spec.host}')
URL="https://${ENTRY_HOST}/"

# Same public URL, request routed to Application A
curl -kfsS -H 'x-app-target: a' "$URL"

# Same public URL, request routed to Application B
curl -kfsS -H 'x-app-target: b' "$URL"

# No header: default to A
curl -kfsS "$URL"

watch -n 2 -c -- 'URL="https://$(oc get route full-single-service -n full-single-service -o jsonpath="{.spec.host}")/"; for app in a b; do curl -kfsS -H "x-app-target: $app" "$URL"; echo; done | sed -e "s/BLUE/\x1b[94mBLUE\x1b[0m/g" -e "s/YELLOW/\x1b[93mYELLOW\x1b[0m/g"'
```

Representative responses (ConfigMap identifiers stay constant; counters vary):

```text
Application A - BLUE greeter => 'greeter-a-8f1c4e' : 1
Application A - BLUE greeter => 'greeter-a-8f1c4e' : 2
Application B - BLUE greeter => 'greeter-b-3d7a92' : 1
```

Example traffic generator for Kiali, switching applications by HTTP header:

```bash
for i in $(seq 1 100); do
  curl -kfsS -H 'x-app-target: a' "$URL"
  curl -kfsS -H 'x-app-target: b' "$URL"
  sleep 0.2
done
```

The counters are **per pod** and reset on pod replacement. During a canary,
one extra pod per updating Rollout can exist temporarily, each with its own
counter; AnalysisJobs can increment the candidate's counter too. No HTTP
readiness/liveness probes are generated because the probes are TCP.

### Verify single-route topology

```bash
oc get routes -n full-single-service
oc get virtualservice full-single-service-router -n full-single-service \
  -o jsonpath='{range .spec.http[*]}{.name}{"\n"}{end}'
oc get rollouts.argoproj.io -n full-single-service
```

The expected OpenShift Route list has **only `full-single-service`**.
The VirtualService must retain both named Istio HTTP routes, `secondary`
followed by `primary` (the A catch-all). Istio matches them in order.

### If header B still reaches A after GitOps sync

Check the *live* VirtualService, not just the declarative Git file. A previously
reported migration left an old hostname-matching VirtualService in place, which
caused `x-app-target: b` requests to reach A. Use the read-only check first:

```bash
bash scripts/reconcile-full-single-service-routing.sh check
```

Once both Greeter Rollouts have Ready pods and the GitOps revision is visible,
apply the guarded routing-only fix if needed:

```bash
bash scripts/reconcile-full-single-service-routing.sh repair
```

This action refuses an unknown cluster and unexpected destination layout. It
uses the VirtualService definition from `origin/main`, **preserves the live A/B
stable/canary weights**, and updates only the Istio HTTP route order/matches.
It never deletes Services or Routes. The `oc patch` includes a
`resourceVersion` test so concurrent controller edits fail safely.

After the repair, verify both responses via the same neutral Route, as shown
above. If Argo CD immediately overwrites the match rules again, inspect its
Application status and operation events before repeating the repair.


Removing the old HTTPD ConfigMaps from Kustomize means Argo CD may prune them
when the changes are pushed. This modifies only the `full-single-service` demo;
it leaves the original Knative demo untouched.
