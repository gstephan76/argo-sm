#!/usr/bin/env bash
set -Eeuo pipefail
NS=full-multi
for tool in oc jq; do command -v "$tool" >/dev/null || { echo "Missing $tool" >&2; exit 1; }; done
printf '\n== Istio mesh tracing provider ==\n'
oc -n istio-system get istio default -o json | jq '{tracing: .spec.values.meshConfig.enableTracing, rootNamespace: .spec.values.meshConfig.rootNamespace, providers: [.spec.values.meshConfig.extensionProviders[]? | select(.opentelemetry != null) | {name, opentelemetry}]}'
printf '\n== Effective Telemetry objects (inspect mesh root and full-multi) ==\n'
oc get telemetries.telemetry.istio.io -A -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,PROVIDER:.spec.tracing[*].providers[*].name,SAMPLING:.spec.tracing[*].randomSamplingPercentage'
printf '\n== full-multi Rollouts and Pods / injected containers ==\n'
oc get rollouts.argoproj.io -n "$NS"
oc get pods -n "$NS" -o json | jq -r '.items[] | [.metadata.name, (.spec.containers|map(.name)|join(",")), (.status.phase // "?")] | @tsv'
printf '\n== full-multi routing and metric targets ==\n'
oc get route,virtualservice,podmonitor -n "$NS" 2>&1 || true
printf '\n== Collector and TempoStack ==\n'
oc get opentelemetrycollectors.opentelemetry.io -A
oc get tempostacks.tempo.grafana.com -A
printf '\n== Kiali CR (current tracing configuration; no token disclosure) ==\n'
oc get kialis.kiali.io -A -o json | jq '[.items[] | {namespace: .metadata.namespace, name: .metadata.name, tracing: (.spec.external_services.tracing | if . == null then null else {enabled,provider,internal_url,external_url,tempo_config,use_grpc,auth: {type: .auth.type,use_kiali_token: .auth.use_kiali_token}} end)}]'
printf '\n== Collector trace pipeline / exporter names (no credentials) ==\n'
oc -n istio-system get opentelemetrycollector otel -o json | jq 'if (.spec.config|type)=="object" then {pipelines: .spec.config.service.pipelines, exporter_names:(.spec.config.exporters|keys),receiver_names:(.spec.config.receivers|keys)} else {message:"Collector config is not a JSON object; inspect oc get ... -o yaml"} end'
printf '\n== Guide ==\n'
echo 'Generate traffic: INSTANCE=a REQUESTS=100 bash scripts/full-multi.sh traffic'
echo 'Collector metrics: oc -n istio-system port-forward svc/otel-collector-monitoring 18888:8888'
echo 'curl -s localhost:18888/metrics | grep -E "otelcol_(receiver_accepted_spans|exporter_sent_spans|exporter_send_failed_spans)"'
echo 'Database SQL spans require application instrumentation; mesh proxies alone see MySQL as TCP.'
