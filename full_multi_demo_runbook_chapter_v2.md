## 13. Live demonstration runbook — delegated ingress, canaries, databases, and Tempo

This is the **operator sequence** for the deployed `full-multi` demo: one public
OpenShift Route, one `full-multi-gateway`, a root ingress VirtualService
`full-multi-front-router` delegating to `front-a-router` / `front-b-router`,
independent `backend-{a,b}-router` and `crud-{a,b}-router` mesh VirtualServices,
eight Rollouts, and 17 Kubernetes Services. The root chooses A/B;
Argo Rollouts independently controls the Frontend and Backend canary weights.
MariaDB BLUE/GREEN are separate active/preview database Pods, while the two
CRUD BLUE/GREEN Services for each application select the *same* CRUD Pod.

Commands below work from **Bash or Zsh** (no Bash-only `${app^^}` expansion).
Execute them from the repository root:

```bash
cd ~/Documents/POCs/argo-sm
NS=full-multi
HOST="$(oc -n "$NS" get route full-multi -o jsonpath='{.spec.host}')"
printf 'Demo endpoint: https://%s/\n' "$HOST"
```

**Demo prerequisites:** `oc`, `curl`, `jq`, `openssl`, and a valid OpenShift
login; Argo CD Application `full-multi` synchronized; candidate Frontend,
Backend and MariaDB Pods already running if testing canary/GREEN routes.
For the examples below, BLUE means the *software revision* and GREEN is the
MariaDB *preview track*; do not conflate software color with Service role
once a Rollout has been promoted.

### 13.1 Prepare and start — only when resetting the demonstration

**Skip this subsection if the six lifecycle Rollouts are already paused at
the desired 50% canary / MariaDB preview state.** `prepare` and `start` are
state-changing operations that commit and push to `main`, synchronize GitOps,
and change Rollouts. `prepare` removes completed demo history; MariaDB uses
`emptyDir` and its datasets are disposable. Run **serially**, never
concurrently:

```bash
# For a fresh demonstration/reset only; not during an active demo.
INSTANCE=a bash scripts/full-multi.sh prepare
INSTANCE=b bash scripts/full-multi.sh prepare
INSTANCE=a bash scripts/full-multi.sh start
INSTANCE=b bash scripts/full-multi.sh start
```

Confirm Argo CD synchronization and the six paused lifecycle Rollouts:

```bash
oc -n openshift-gitops get application full-multi \
  -o jsonpath='{.status.sync.revision}{"\n"}{.status.sync.status}{"\n"}'
git rev-parse HEAD

oc -n full-multi get rollouts.argoproj.io \
  front-a backend-a mariadb-a front-b backend-b mariadb-b \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,STEP:.status.currentStepIndex,STABLE:.status.stableRS,CURRENT:.status.currentPodHash'
```

Expected: Frontend and Backend A/B `Paused` at step `6`; MariaDB A/B
`Paused` with active and preview revisions. Do **not** promote MariaDB if the
demo must show the BLUE and GREEN datasets concurrently.

### 13.2 Show the root/delegate control plane and the live canary weights

```bash
# Root -> delegates (A is the default; x-app-target:b selects B).
oc -n full-multi get virtualservice full-multi-front-router -o json |
  jq -r '.spec.http[] | select(.delegate != null) |
    "\(.name) -> \(.delegate.name)"'

# Rollouts' independently managed Frontend weights (expect 50/50 at pause).
for app in a b; do
  echo "=== Frontend $app ==="
  oc -n full-multi get virtualservice "front-${app}-router" -o json |
    jq -r '.spec.http[] |
      select(.name == "primary" or .name == "secondary") |
      .route[] | "\(.destination.host): \(.weight)%"'
done

# Backend weight is separately managed inside the service mesh.
for app in a b; do
  echo "=== Backend $app ==="
  oc -n full-multi get virtualservice "backend-${app}-router" -o json |
    jq -r --arg route "backend-${app}-default" '
      .spec.http[] | select(.name == $route) |
      .route[] | "\(.destination.host): \(.weight)%"'
done
```

The root is attached to `full-multi-gateway`; its delegates intentionally
have **no** `hosts` or `gateways`. Internal Backend and CRUD VirtualServices
use `gateways: [mesh]`. Delegation is control-plane composition, **not** an
extra network hop or a chain of Envoy proxies.

### 13.3 Single request — show all release and database evidence

The header spelling is `x-front-track`, **not** `x-frontend-track`.
A typo silently falls back to weighted routing.

```bash
curl -kfsS \
  -H 'x-app-target: a' \
  -H 'x-front-track: canary' \
  -H 'x-backend-track: stable' \
  -H 'x-db-track: green' \
  "https://${HOST}/api/books" |
  jq '{
    application,
    frontend_release,
    frontend_color: .frontend_accent,
    frontend_pod,
    backend_release,
    backend_color: .backend_accent,
    backend_pod,
    database_track,
    database_host,
    first_book: .books[0],
    book_count: (.books | length)
  }'
```

Expected for the candidate Frontend/stable Backend/GREEN database before
promotion: Frontend `YELLOW`, Backend `BLUE`, database track `canary`, first
title `Memórias Póstumas de Brás Cubas`, and 20 books. BLUE database requests
return `Pride and Prejudice` (20 books).

### 13.4 Exercise all 16 combinations once (four dimensions)

There are **three nested routing dimensions** (`front`, `backend`, `db`) for
each of the two applications A/B, giving **2 × 2 × 2 × 2 = 16** combinations.
The output includes the actual pod identities, not just requested headers.

```bash
for app in a b; do
  for ft in stable canary; do
    for bt in stable canary; do
      for db in blue green; do
        curl -kfsS \
          -H "x-app-target: $app" \
          -H "x-front-track: $ft" \
          -H "x-backend-track: $bt" \
          -H "x-db-track: $db" \
          "https://${HOST}/api/books" |
        jq -r --arg ft "$ft" --arg bt "$bt" --arg db "$db" '
          [
            .application, $ft, $bt, $db,
            .frontend_release, .backend_release, .database_track,
            .books[0].title, .frontend_pod, .backend_pod
          ] | @tsv'
      done
    done
  done
done
```

For a stronger automated assertion, run the delegation migration's regression
script **if it exists in this checkout** (it was delivered in the migration
archive and is not part of the tracked repository at the time of writing):

```bash
if test -f smoke_full_multi_front_delegation_v1.sh; then
  bash smoke_full_multi_front_delegation_v1.sh
else
  echo 'Missing smoke_full_multi_front_delegation_v1.sh: extract it from the delegation migration archive.' >&2
fi
```

Expected: `PASS: root and delegates active, both Frontend Rollouts retargeted,
17 Services`, followed by `PASS` for all 16 combinations.

### 13.5 Sustained load for the Kiali workload graph (three routing loops)

This repeatedly exercises **Frontend stable/canary × Backend stable/canary ×
database BLUE/GREEN** within each application. With
`REQUESTS_PER_COMBINATION=10`, the total is **160 requests**. Increase it to
20 for 320 requests. Requests are sequential to keep the command simple.

```bash
REQUESTS_PER_COMBINATION=10
for app in a b; do
  for ft in stable canary; do
    for bt in stable canary; do
      for db in blue green; do
        ok=0
        for i in $(seq 1 "$REQUESTS_PER_COMBINATION"); do
          if curl -kfsS -o /dev/null \
            -H "x-app-target: $app" \
            -H "x-front-track: $ft" \
            -H "x-backend-track: $bt" \
            -H "x-db-track: $db" \
            "https://${HOST}/api/books"
          then
            ok=$((ok + 1))
          fi
        done
        printf 'app=%s front=%s backend=%s db=%s HTTP_OK=%s/%s\n' \
          "$app" "$ft" "$bt" "$db" "$ok" "$REQUESTS_PER_COMBINATION"
      done
    done
  done
done
```

In Kiali, select namespace `full-multi`, **Workload graph**, and a time window
covering the load (for example Last 5 minutes). The graph shows the actual
Frontend, Backend, CRUD, and MariaDB workload paths. It does **not** display
the root/delegate hierarchy as extra traffic nodes; those are routing rules,
not extra Pods. Both `crud-{a,b}-blue` and `crud-{a,b}-green` Services can
appear, even though each pair selects the same CRUD Pod.

### 13.6 Demonstrate automatic 50/50 weighted routing

**Omit** `x-front-track` (and optionally `x-backend-track`) to let Istio use
Argo Rollouts-managed weights. Sending an empty or misspelled header is less
clear than omitting it.

```bash
for app in a b; do
  printf '=== Application %s: 200 requests (Frontend weighted) ===\n' \
    "$(printf '%s' "$app" | tr '[:lower:]' '[:upper:]')"

  for i in $(seq 1 200); do
    curl -kfsS \
      -H "x-app-target: $app" \
      -H 'x-backend-track: stable' \
      -H 'x-db-track: green' \
      "https://${HOST}/api/books" |
    jq -r '.frontend_release'
  done | sort | uniq -c
done
```

Expect **approximately** 100 BLUE / 100 YELLOW per application, not an exact
alternation. A sample of 20 requests can differ markedly from 50/50.

To exercise both **independent** Frontend and Backend weighted canaries, omit
both track headers and count the four combinations:

```bash
for app in a b; do
  echo "=== Independent HTTP canary combinations: $app ==="
  for i in $(seq 1 200); do
    curl -kfsS -H "x-app-target: $app" -H 'x-db-track: blue' \
      "https://${HOST}/api/books" |
      jq -r '"\(.frontend_release)/\(.backend_release)"'
  done | sort | uniq -c
done
```

At 50/50 on both tiers, expect roughly **25%** in each of BLUE/BLUE,
BLUE/YELLOW, YELLOW/BLUE, and YELLOW/YELLOW.

### 13.7 Generate an identifiable distributed trace

A W3C `traceparent` gives you an exact trace ID to retrieve from Tempo.
This example requests **Application A / Frontend canary / Backend canary /
BLUE database**:

```bash
TRACE_ID="$(openssl rand -hex 16)"
SPAN_ID="$(openssl rand -hex 8)"

curl -kfsS \
  -H 'x-app-target: a' \
  -H 'x-front-track: canary' \
  -H 'x-backend-track: canary' \
  -H 'x-db-track: blue' \
  -H "traceparent: 00-${TRACE_ID}-${SPAN_ID}-01" \
  "https://${HOST}/api/books" |
  jq '{
    application,
    frontend_release,
    backend_release,
    database_host,
    first_title: .books[0].title
  }'

echo "TRACE_ID=$TRACE_ID"
```

Expected: `YELLOW` Frontend, `YELLOW` Backend, MariaDB
`mariadb-a-stable.full-multi.svc.cluster.local`, and first title
`Pride and Prejudice`, provided the BLUE active database has not been
promoted or replaced.

To retrieve that trace, first open a **separate terminal** and keep the
port-forward running:

```bash
oc -n tempo port-forward svc/tempo-mesh-gateway 18080:8080
```

Back in the terminal where `TRACE_ID` is defined, authenticate to the `mesh`
Tempo tenant and fetch the exact trace (the service-account token is
short-lived; do not print or commit it):

```bash
KIALI_TOKEN="$(oc -n istio-system create token \
  kiali-service-account --duration=10m)"

curl -ksS --fail \
  -H "Authorization: Bearer ${KIALI_TOKEN}" \
  -o /tmp/full-multi-tempo-trace.json \
  -w 'Tempo HTTP: %{http_code}\n' \
  "https://127.0.0.1:18080/api/traces/v1/mesh/tempo/api/traces/${TRACE_ID}"

jq '{
  batches: (.batches | length),
  spans: ([.batches[] | (.scopeSpans // .instrumentationLibrarySpans // [])[] |
           .spans[]?] | length),
  services: ([.batches[] | .resource.attributes[]? |
              select(.key == "service.name") | .value.stringValue] | unique)
}' /tmp/full-multi-tempo-trace.json
```

A working HTTP path can show seven Envoy spans across the ingress gateway,
Frontend, Backend, and CRUD; the native MariaDB SQL connection does not
appear as an HTTP span. A `200` response by itself is not enough: verify the
`spans` count is positive and inspect the service names.

### 13.8 Search the traces in Tempo and verify the Collector

With the Tempo port-forward still active:

```bash
curl -ksSG --fail \
  -H "Authorization: Bearer ${KIALI_TOKEN}" \
  --data-urlencode 'q={ resource.service.name = "backend-a.full-multi" }' \
  --data-urlencode "start=$(($(date +%s) - 1800))" \
  --data-urlencode "end=$(date +%s)" \
  --data-urlencode 'limit=30' \
  'https://127.0.0.1:18080/api/traces/v1/mesh/tempo/api/search' |
  jq '{trace_count: (.traces | length),
       trace_ids: [.traces[]?.traceID]}'
```

For Collector counters, start another terminal with:

```bash
oc -n istio-system port-forward svc/otel-collector-monitoring 18888:8888
```

Then, from your main terminal, inspect spans accepted and sent:

```bash
curl -fsS http://127.0.0.1:18888/metrics |
  grep -E '^otelcol_(receiver_accepted_spans|exporter_sent_spans|exporter_send_failed_spans)(\{|[[:space:]]|_total)'
```

Compare counters **before and after** additional requests; they are
cumulative. If either port-forward exits, restart it in its own terminal;
`curl: (7) Failed to connect` to `127.0.0.1` does not, by itself, indicate a
Tempo or Collector outage.

### 13.9 Wrap up and preserve the demonstration state

```bash
oc -n full-multi get virtualservice
oc -n full-multi get svc --no-headers | wc -l   # 17 expected
oc -n full-multi get rollouts.argoproj.io \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,STEP:.status.currentStepIndex'
oc -n full-multi get analysisruns.argoproj.io
```

The main demo should end with Frontend/Backend A/B paused at 50% and both
MariaDB active/preview datasets present. **Do not run**
`INSTANCE=a bash scripts/full-multi.sh promote` unless you intend to promote
*all three* lifecycle tiers, **including MariaDB**, which can discard the
old ephemeral database dataset. Header-forced routing verifies all
combinations; weighted routing validates probabilistic traffic splitting;
Tempo/Kiali validates observability. These are three distinct checks.
