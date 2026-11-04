# `full-multi` fresh OpenShift cluster bootstrap and diagnosis

This is an operator-oriented runbook for a **fresh** OpenShift cluster. The demo
application is reconciled by Argo CD, while OpenShift Service Mesh, Argo Rollouts,
OpenTelemetry, Tempo and Kiali are **platform resources**. Do not add shared
platform CRs to `full-multi/kustomization.yaml`.

## 1. Platform prerequisites (before application deploy)

Ensure OpenShift GitOps, the Service Mesh/Istio control plane (with injection),
Tempo, the Collector and Kiali are installed as required for your demonstration.
Confirm the Argo Rollouts Operator is installed **and has instantiated a controller**.
Having `rollouts.argoproj.io` CRDs is not sufficient.

```bash
oc get rolloutmanager -A
oc get deployment -A | grep -Ei 'argo-rollouts|NAMESPACE'
oc get istios.sailoperator.io -A
oc get opentelemetrycollectors.opentelemetry.io -A
oc get tempostacks.tempo.grafana.com -A
oc get kialis.kiali.io -A
```

If there is no suitable Rollouts controller, use this **explicit, one-time**
platform step (not part of `deploy`):

```bash
oc apply -f platform-monitoring/rollout-manager-cluster.yaml
oc -n openshift-gitops rollout status deployment/argo-rollouts --timeout=180s
oc -n openshift-gitops get rolloutmanager rollout-manager -o json |
  jq '{phase:.status.phase,controller:.status.rolloutController}'
```

The provided manifest creates a **cluster-scoped** manager
(`spec.namespaceScoped: false`). It is appropriate for the multi-namespace Argo
Rollouts demos. If a managed, working cluster-scoped controller already exists,
do **not** install another. If the Operator is configured for namespace-scoped
mode, investigate and reconcile that configuration first; do not force two
controllers with overlapping scope. The `deploy` preflight never installs one.

A new client workstation must also provide the `kubectl-argo-rollouts` plugin
in `PATH` before lifecycle commands using `oc argo rollouts promote`:

```bash
command -v kubectl-argo-rollouts
oc argo rollouts version
```

This CLI is not the controller. `deploy` does not require it, but `prepare`,
`start` and `promote` do.

## 2. Database credentials — deliberately outside Git

Create the namespace and provision two Secrets **before** `deploy`. From Zsh:

```zsh
(
  read -rs 'FULL_MULTI_DB_PASSWORD?MariaDB application password: '; echo
  read -rs 'FULL_MULTI_DB_ROOT_PASSWORD?MariaDB root password: '; echo
  export FULL_MULTI_DB_PASSWORD FULL_MULTI_DB_ROOT_PASSWORD
  bash scripts/full-multi-secrets.sh
)
oc -n full-multi get secrets db-credentials-a db-credentials-b
```

The Secrets must have keys `password` and `root-password`; the script never
prints their values and never stores them in Git. Rerunning the script with
both Secrets already present leaves them **untouched**, even if shell variables
are unset. A partial state (one Secret present, one missing) fails closed.

**Do not rotate or regenerate the Kubernetes passwords while MariaDB is already
initialized.** Kubernetes Secret updates do not automatically change internal
MariaDB user passwords. Data is stored on `emptyDir`: replacing a MariaDB Pod
can destroy the demo dataset.

## 3. Deploy and wait for actual readiness

```bash
cd ~/Documents/POCs/argo-sm
python3 scripts/full-multi-spec.py verify
INSTANCE=a bash scripts/full-multi.sh deploy
INSTANCE=a bash scripts/full-multi.sh status
```

`deploy` is deliberately non-destructive to the databases and Rollout history.
It performs a read-only controller/Secret preflight, applies the Argo CD
Application, and checks these **independent stages**:

1. GitOps revision equals `git rev-parse HEAD` and Argo CD reports `Synced`.
2. `Deployment/full-multi-ingressgateway` is available.
3. All **eight** Rollouts (Front A/B, Backend A/B, CRUD A/B, MariaDB A/B)
   have current observed generations, available replicas and `Healthy` phases.
4. The external Route has a hostname.

The scripts show state changes and fail with actionable errors on terminal
sync failures, Degraded Rollouts, or readiness timeout. Argo CD `Synced` is **not**
equivalent to `Healthy`, and `Healthy` still does not prove HTTP traffic or
trace ingestion.

For diagnostics:

```bash
oc -n openshift-gitops get application full-multi -o json |
  jq '{revision:.status.sync.revision,sync:.status.sync.status,
       health:.status.health.status,operation:.status.operationState,
       conditions:.status.conditions}'
oc -n full-multi get deployment,rollouts.argoproj.io,replicasets,pods
oc -n full-multi get events --sort-by=.lastTimestamp | tail -30
oc -n full-multi describe rollout mariadb-a
```

If all eight Rollout CRs exist but `.status` remains `null` and none has a
ReplicaSet, check the **Rollouts controller**, not GitOps or Secret content:

```bash
oc get rolloutmanager -A
oc -n openshift-gitops get deployment argo-rollouts
oc -n openshift-gitops logs deployment/argo-rollouts --since=5m
```

If the ingress Pod is rejected with `port 15090 is reserved for Istio`, do not
reintroduce `prometheus.io/port: '15090'` in authored Pod templates. It is
reserved for Envoy. Let the injector provide the effective scrape annotations
(`prometheus.io/scrape=true`, `port=15020`, `path=/stats/prometheus` on this
OSSM installation). The existing PodMonitor discovers the injected Istio
proxy container and annotation-based target; it should not scrape Envoy
through an application-authored reserved port.

```bash
oc -n full-multi get pods -o json |
  jq -r '.items[] | [.metadata.name,
    (.metadata.annotations["prometheus.io/port"] // "MISSING"),
    (.metadata.annotations["prometheus.io/path"] // "MISSING")] | @tsv'
```

## 4. Application end-to-end request test

```bash
HOST="$(oc -n full-multi get route full-multi -o jsonpath='{.spec.host}')"
curl -kfsS \
  -H 'x-app-target: a' \
  -H 'x-front-track: stable' \
  -H 'x-backend-track: stable' \
  -H 'x-db-track: stable' \
  "https://${HOST}/api/books" |
  jq '{application,frontend_release,backend_release,database_host,first_book}'
```

A fresh cluster deploys **whatever BLUE/YELLOW manifest revisions are currently
on GitHub `main`**. It does not silently reset both applications to BLUE. Inspect
MariaDB active image and `demo-rollout-revision` before running `prepare`.
`prepare` and `promote` can replace `emptyDir` MariaDB Pods; use only as a
conscious demo lifecycle action.

## 5. Why Kiali shows two CRUD paths and two MariaDB paths

For each A/B instance, `crud-*-blue` and `crud-*-green` are **two Services
with the same `app: crud-*` selector**, not independently blue/green CRUD Pods.
`crud-*-router` sets `x-crud-db-target` per `x-db-track`; the single CRUD
workload then opens native SQL connections to MariaDB active or preview.

Conversely, `mariadb-*-stable` and `mariadb-*-canary` are the **active and
preview Services** of a real Argo Rollouts BLUE/GREEN database strategy. With
an actual preview revision, they may select different MariaDB Pods and
**different ephemeral datasets**. Without a preview, both Services can point
to the same current Pod. Two Kiali service nodes therefore do not prove two
application deployments. Inspect Service endpoint selectors and actual Pods.

## 6. Tempo/Kiali — separate observability acceptance checks

`full-multi` GitOps sync does not validate distributed trace ingestion. Keep
the shared Collector, TempoStack and Kiali CR under their platform owners.
The optional namespace enrichment workflow is in
[`platform-monitoring/README-otel-full-multi.md`](../platform-monitoring/README-otel-full-multi.md)
and its explicit opt-in script is `scripts/full-multi-otel-k8sattributes.py`.

Kiali 2.27 uses a qualified service lookup, e.g. `front-a.full-multi`, when
`namespace_selector: true`. In the observed Tempo tenant, the `/api/echo` and
`/api/status/buildinfo` endpoints returned **403**, while a tenant-authorized
TraceQL `/api/search` returned **200**. The Kiali configuration tool now
proposes `disable_version_check: true` and a permission-verified search health
URL. Run it with explicit tenant selection and review its plan first; do not
apply settings blindly to a shared Kiali instance.

```bash
python3 scripts/full-multi-kiali-tempo.py --tenant mesh
# Apply only after reviewing the exact target CR and current tenant permissions:
# python3 scripts/full-multi-kiali-tempo.py --tenant mesh --apply --replace-existing
bash scripts/check-full-multi-tracing.sh
```

Generate new HTTP traffic with a W3C `traceparent`, then test Tempo both by
exact trace ID and **within the same time window** using qualified
`resource.service.name` and `resource.k8s.namespace.name` searches. An empty
`/api/search` response can contain `metrics` but no `traces`; this means zero
matches, not invalid JSON. A newly submitted trace can also take time to
become retrievable. Avoid diagnosing Kiali from namespace-only queries without
first verifying the expected service-name search and Kiali's actual API query.

The MariaDB connection is native SQL/TCP, not an HTTP mesh span. HTTP proxy
traces typically end at CRUD unless application/database tracing is added.
