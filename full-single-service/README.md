# full-single-service: two Istio-weighted HTTPD canaries (Knative demo image)

This directory is managed by the **existing** Argo CD Application `full-single-service`
(whose source path is `full-single-service/`), in namespace `full-single-service`.
It is not a Knative Service. Two standard Argo Rollouts, A and B, run the same
**Red Hat UBI9 HTTPD image** used by `knative/app/service.yaml` in the original
`gstephan76/argocd-bluegreen` repository:

`registry.access.redhat.com/ubi9/httpd-24:latest`

As with the original Knative demo, the application serves an HTML page mounted
from an immutable ConfigMap at `/var/www/html/index.html`. The shared
`httpd-page-v1` and `httpd-page-v2` ConfigMaps provide the same V1/V2 page
concept, adapted to describe an Argo Rollouts deployment rather than Knative.
Both A and B select their own page ConfigMap independently.

| Resource | Instance A | Instance B |
|---|---|---|
| Kubernetes Rollout | `full-single-service-a` | `full-single-service-b` |
| YAML | `rollout-a.yaml` | `rollout-b.yaml` |
| Public OpenShift Route | `full-single-service-a` | `full-single-service-b` |
| Stable Service | `full-single-service-a-stable` | `full-single-service-b-stable` |
| Canary Service | `full-single-service-a-canary` | `full-single-service-b-canary` |
| AnalysisTemplate | `full-single-service-a-smoke-test` | `full-single-service-b-smoke-test` |
| Istio route name | `primary` | `secondary` |

All resources share a single namespace, Istio ingress gateway, and VirtualService.
Independent HTTPRoute weights for primary and secondary are managed by Argo
Rollouts; Argo CD must ignore those dynamic weights and each managed Service's
`rollouts-pod-template-hash` selector.

## Canary workflow

Each instance uses V1 as the baseline and V2 as the candidate, with traffic
weights 10/25/50/75/100% followed by manual promotion. The candidate is checked
at every weight by a short-lived Kubernetes AnalysisJob that GETs `/` through
the *candidate Service* and verifies `Revision V1` or `Revision V2` in the HTML.
Both instances use TCP readiness and liveness probes on port 8080; there are no
HTTP readiness/liveness requests. Both pages use the **same container image**;
changing the immutable ConfigMap reference and rollout revision annotation
creates the new candidate ReplicaSet.

```bash
# after changes are merged/reconciled to main
INSTANCE=a bash scripts/full-single-service.sh prepare
INSTANCE=a bash scripts/full-single-service.sh start
INSTANCE=a bash scripts/full-single-service.sh promote

INSTANCE=b bash scripts/full-single-service.sh prepare
INSTANCE=b bash scripts/full-single-service.sh start
INSTANCE=b bash scripts/full-single-service.sh promote
```

Do not execute prepare/start/promote commands simultaneously: they commit desired
state changes to the same Git branch. Run them one at a time and check that each
command succeeds before issuing the next.

Generate Kiali-visible traffic on either independent hostname:

```bash
INSTANCE=a REQUESTS=300 TRAFFIC_INTERVAL_SECONDS=0.2 bash scripts/full-single-service.sh traffic
INSTANCE=b REQUESTS=300 TRAFFIC_INTERVAL_SECONDS=0.2 bash scripts/full-single-service.sh traffic
```

Kiali should display inbound generator HTTP requests to the two HTTPD backends
and their ingress gateway. HTTPD does not generate downstream application calls,
and the TCP probes do not create HTTP requests. During a rollout, the five
AnalysisJobs do generate a small number of HTTP requests: for a generator-only
Kiali time window, start the generators after the analysis runs complete and
filter out the earlier period. Other independent HTTP callers can also appear.
The PodMonitor requests metrics from the injected Istio proxies via port 15090;
Kiali must use the matching Prometheus/Thanos data source.

## Names and cleanup

This version replaces the old unqualified A names, including
`full-single-service-stable`, `full-single-service-canary`, the old
`full-single-service` Rollout, Route, and AnalysisTemplate. A's new pod selector
uses `app=full-single-service-a`; B's uses `app=full-single-service-b`.
Renaming Kubernetes resources is a replacement, **not** an in-place rename.
The existing auto-synced Argo CD Application will create and prune resources on
`main`; A traffic may temporarily be interrupted. The Application definition
at `argocd/application-full-single-service.yaml` lives outside the Argo CD
source directory and needs to be applied separately, since its Service ignore
rules must now match the explicit A/B names. Verify A and B have Ready pods
and that each public hostname serves an HTML Revision V1 or V2 before cleaning
orphaned ReplicaSets, jobs and obsolete old-A resources.

The external Knative `knative-httpd` namespace and its resources are not touched.
