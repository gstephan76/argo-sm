# Tempo namespace filtering: `full-multi` Kubernetes resource attributes

## Scope and baseline

This **platform-level** optional patch adds `k8s.namespace.name`, `k8s.pod.name`,
and `k8s.pod.uid` to *new* `full-multi` spans via the OpenTelemetry Collector
`istio-system/otel`. It does **not** modify the `full-multi` Argo CD Application,
any workload/Pod template, Istio routing, TempoStack, or exporter settings.

Patch prepared against `gstephan76/argo-sm`, GitHub `main` at
`c81a8b3ecf5cb2adaaae81ff684f3e7a94b42e68` (2026-10-29).
GitLab `glauco.stephan/argo-sm` and `gstephan76/argo-sm` were not available
through the connected GitLab account. **This is not a verified GitLab rebase.**
If another GitLab repository is authoritative, verify its path and baseline
before carrying this patch there.

Observed pre-patch cluster configuration:

- `OpenTelemetryCollector/otel` in `istio-system`, mode Deployment, ServiceAccount
  `istio-system/otel-collector`.
- Existing traces pipeline: `otlp -> memory_limiter -> batch -> otlp_grpc/tempo`.
- Export endpoint: `tempo-mesh-gateway.tempo.svc.cluster.local:8090`, tenant
  `mesh` (as configured by the existing exporter).
- Existing span resources have `service.name` but no `k8s.namespace.name`.
- `otel-collector` ServiceAccount had no Pod discovery RBAC in `full-multi`.

The fix is intentionally limited to namespace-scoped Pod discovery. The
processor attempts association first using an existing `k8s.pod.ip` resource
attribute, then using the incoming OTLP connection IP. If the proxy/connection
IP cannot be associated with a Pod, that span may remain unenriched. Do **not**
falsify namespace attributes from the service-name suffix.

## Install from the repository root

```bash
cd ~/Documents/POCs/argo-sm
oc whoami --show-context
python3 scripts/full-multi-otel-k8sattributes.py --check
```

The script refuses unexpected Collector pipeline, exporter endpoint,
ServiceAccount, RBAC ownership, or processor configurations. It does not rely
on the `kubectl.kubernetes.io/last-applied-configuration` annotation (which may
be stale after API patching).

Before changing the shared Collector, save a backup outside Git:

```bash
oc -n istio-system get opentelemetrycollector otel -o yaml \
  > /tmp/otel-before-full-multi-k8sattributes.yaml
oc -n full-multi get role,rolebinding -o yaml \
  > /tmp/full-multi-rbac-before-otel.yaml
```

Review namespace-scoped RBAC:

```bash
cat platform-monitoring/otel-full-multi-rbac.yaml
```

Then enable explicitly:

```bash
python3 scripts/full-multi-otel-k8sattributes.py --apply
oc -n istio-system rollout status deployment/otel-collector --timeout=180s
python3 scripts/full-multi-otel-k8sattributes.py --verify
```

The processor chain becomes `memory_limiter -> k8sattributes -> batch`, so
connection information is available before batching. Namespace-scoped RBAC
allows `get/list/watch` on **Pods only** in `full-multi`. This minimal profile
extracts only Pod metadata, not Namespace/ReplicaSet/Node labels or owner
information. Collector operator may restart the Collector deployment, causing
a brief tracing export interruption. It should not restart app workloads.

Check the Collector logs on failure:

```bash
oc -n istio-system logs deployment/otel-collector --since=5m \
  | grep -Ei 'k8sattributes|error|forbidden|permission|fail' || true
```

## Verify newly ingested traces (not historical ones)

In one terminal:

```bash
oc -n tempo port-forward svc/tempo-mesh-gateway 18080:8080
```

In another terminal:

```bash
HOST="$(oc -n full-multi get route full-multi -o jsonpath='{.spec.host}')"
TRACE_ID="$(openssl rand -hex 16)"
SPAN_ID="$(openssl rand -hex 8)"

curl -kfsS -H 'x-app-target: a' \
  -H 'x-front-track: stable' \
  -H 'x-backend-track: stable' \
  -H 'x-db-track: green' \
  -H "traceparent: 00-${TRACE_ID}-${SPAN_ID}-01" \
  "https://${HOST}/api/books" | jq '{application,frontend_release,backend_release,database_host}'

KIALI_TOKEN="$(oc -n istio-system create token kiali-service-account --duration=10m)"
curl -kfsS -H "Authorization: Bearer ${KIALI_TOKEN}" \
  "https://127.0.0.1:18080/api/traces/v1/mesh/tempo/api/traces/${TRACE_ID}" \
  -o /tmp/otel-full-multi-new-trace.json

jq -r '
  .batches[]
  | ([.resource.attributes[]? | select(.key == "service.name")
      | .value.stringValue] | first // "unknown") as $service
  | ([.resource.attributes[]? | select(.key == "k8s.namespace.name")
      | .value.stringValue] | first // "MISSING") as $namespace
  | [$service, $namespace, ([.scopeSpans[]?.spans[]?] | length)]
  | @tsv
' /tmp/otel-full-multi-new-trace.json
```

Tempo can initially return a *partial trace*. If needed, retrieve the same
trace ID again to see the remaining spans.

Then in Tempo tenant `mesh`, search:

```traceql
{ resource.k8s.namespace.name = "full-multi" }
```

If this query still finds nothing, check OTLP peer-IP association before
modifying RBAC or your application. The `service.name` query remains a useful
control:

```traceql
{ resource.service.name = "backend-a.full-multi" }
```

This patch addresses namespace attributes; it does not guarantee a given UI's
namespace selector uses the same attribute.

## Rollback

```bash
python3 scripts/full-multi-otel-k8sattributes.py --rollback
oc -n istio-system rollout status deployment/otel-collector --timeout=180s
```

RBAC is intentionally **not deleted** by rollback. After confirming no other
consumer needs it, remove the patch-owned resources separately:

```bash
oc -n full-multi delete rolebinding otel-full-multi-reader
oc -n full-multi delete role otel-full-multi-reader
```

The script's rollback uses Kubernetes JSON Patch with resourceVersion and
configuration tests to avoid silently overwriting concurrent operator edits.
If the Collector is managed by a separate platform GitOps source, port this
change to that authoritative repository; direct patches can be reverted by
future `oc apply` or other reconcilers.
