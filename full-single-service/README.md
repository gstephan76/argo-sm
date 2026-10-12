# full-single-service — Application A/B BLUE → YELLOW Istio canaries

This is an Argo CD + Argo Rollouts + OpenShift Service Mesh demo in namespace
`full-single-service`, **not a Knative Service**. Both Rollouts deploy the same
prebuilt Quarkus greeter used in the Knative tutorial:

`quay.io/rhdevelopers/knative-tutorial-greeter:quarkus`

The greeter supports a `MESSAGE_PREFIX` environment variable and already returns
a pod-local, incrementing request count. No custom counter server, image build,
HTML page, or ConfigMap volume is needed.

## Responses

`GET /` responds with one plain-text line like:

```text
Application A - BLUE greeter => 'pod-a-blue' : 1
Application A - BLUE greeter => 'pod-a-blue' : 2
Application B - YELLOW greeter => 'pod-b-yellow' : 1
```

Exact whitespace and hostname formatting are owned by the upstream greeter. The
`Application X - COLOR` prefix is supplied through `MESSAGE_PREFIX`. The count
is maintained per pod: it resets if that pod is replaced and may jump from the
point of view of a client when Istio routes requests between BLUE/YELLOW pods.
Canary AnalysisJobs and any other HTTP callers also increment the counter.

Readiness and liveness probes use TCP on port 8080, avoiding HTTP health-check
requests being included in traffic counts. The greeter does not make downstream
calls, so Kiali will mainly show traffic generator → Istio ingress → A/B pods.
During active canary analysis, the short-lived AnalysisJobs make HTTP requests
as well; choose a later Kiali time window to isolate generator traffic.

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
```

Representative responses (pod ID and counter will vary):

```text
Application A - BLUE greeter => 'pod-a' : 1
Application A - BLUE greeter => 'pod-a' : 2
Application B - BLUE greeter => 'pod-b' : 1
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

Once both Quarkus Rollouts have Ready pods and the GitOps revision is visible,
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
