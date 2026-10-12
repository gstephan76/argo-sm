#!/usr/bin/env bash
set -Eeuo pipefail

# Explicit demo commands only. Never used by the Git-only migration workflow.
NS="full-single-service"
APP="full-single-service"        # single Argo CD Application owns both Rollouts
INSTANCE="${INSTANCE:-a}"
case "$INSTANCE" in
  a) ROLLOUT="full-single-service-a"; ROUTE="full-single-service-a"; ROLLOUT_FILE="full-single-service/rollout-a.yaml" ;;
  b) ROLLOUT="full-single-service-b"; ROUTE="full-single-service-a"; ROLLOUT_FILE="full-single-service/rollout-b.yaml" ;;
  *) echo "ERROR: INSTANCE must be a or b" >&2; exit 1 ;;
esac
ARGOCD_NS="${ARGOCD_NAMESPACE:-openshift-gitops}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-600}"
POLL_SECONDS="${POLL_SECONDS:-5}"
OC_REQUEST_TIMEOUT="${OC_REQUEST_TIMEOUT:-12s}"
OC_WALL_TIMEOUT_SECONDS="${OC_WALL_TIMEOUT_SECONDS:-20}"
EXPECTED_ORIGIN="https://github.com/gstephan76/argo-sm.git"

fatal() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fatal "Missing command: $1"; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
command="${1:-help}"

require_git_head() {
  need git
  [[ "$(git rev-parse --show-toplevel)" == "$ROOT" ]] || fatal "Run in a checkout rooted at $ROOT"
  branch="$(git branch --show-current)"
  [[ "$branch" == main ]] || fatal "This demo's Argo CD Application targets main; current branch: ${branch:-detached}"
  [[ "$(git remote get-url origin)" == "$EXPECTED_ORIGIN" ||
     "$(git remote get-url origin)" == "https://github.com/gstephan76/argo-sm" ||
     "$(git remote get-url origin)" == "git@github.com:gstephan76/argo-sm.git" ]] ||
    fatal "origin does not point to gstephan76/argo-sm"
  git diff --quiet && git diff --cached --quiet || fatal "Tracked modifications present; commit or restore first"
  git fetch origin main
  [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] ||
    fatal "HEAD must equal origin/main (no behind/ahead commits)"
}

require_cluster() {
  need oc
  oc whoami >/dev/null || fatal "Not logged in to OpenShift"
  for api in applications.argoproj.io rollouts.argoproj.io \
    analysistemplates.argoproj.io virtualservices.networking.istio.io \
    gateways.networking.istio.io; do
    oc get crd "$api" >/dev/null || fatal "Missing CRD: $api"
  done
  oc wait --for=condition=Ready istios.sailoperator.io/default \
    -n istio-system --timeout=15s >/dev/null || fatal "OSSM Istio/default is not Ready"
  oc wait --for=condition=Ready istiocnis.sailoperator.io/default \
    -n istio-cni --timeout=15s >/dev/null || fatal "OSSM IstioCNI/default is not Ready"
}

# GET operations must not hang indefinitely if the API server/proxy is stuck.
# These are read-only probes; do not suppress permission or connectivity errors.
oc_get_bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout --foreground --signal=TERM "${OC_WALL_TIMEOUT_SECONDS}s" \
      oc --request-timeout="$OC_REQUEST_TIMEOUT" get "$@"
  else
    oc --request-timeout="$OC_REQUEST_TIMEOUT" get "$@"
  fi
}

get_rollout() {
  oc_get_bounded rollouts.argoproj.io "$ROLLOUT" -n "$NS" -o "jsonpath=$1" 2>/dev/null || true
}

get_app() {
  oc_get_bounded applications.argoproj.io "$APP" -n "$ARGOCD_NS" -o "jsonpath=$1" 2>/dev/null || true
}

# Obtain the entire Application in ONE API request per poll so fields describe
# the same reconciliation state. Return a unit-separated line of safe strings.
argocd_snapshot() {
  local response
  if ! response="$(oc_get_bounded applications.argoproj.io "$APP" -n "$ARGOCD_NS" -o json 2>&1)"; then
    echo "ERROR: Unable to read Argo CD Application $ARGOCD_NS/$APP: $response" >&2
    return 1
  fi
  printf '%s' "$response" | python3 -c '
import json, sys
try:
    app = json.load(sys.stdin)
except (ValueError, TypeError) as exc:
    sys.exit("Invalid Argo CD Application response: " + str(exc))
spec = app.get("spec") or {}
status = app.get("status") or {}
sync = status.get("sync") or {}
operation = status.get("operationState") or {}
health = status.get("health") or {}
conditions = status.get("conditions") or []
source = spec.get("source") or {}
source_repo = source.get("repoURL") or ""
errors = [c for c in conditions if c.get("type") in (
    "ComparisonError", "InvalidSpecError", "SyncError", "UnknownError")]
def clean(value):
    return str(value or "").replace("\x1f", " ").replace("\n", " ").replace("\r", " ")[:500]
summary = "; ".join((c.get("type", "?") + ": " + c.get("message", "")) for c in conditions)
operation_message = operation.get("message") or ""
print("\x1f".join(map(clean, (
    source_repo, sync.get("status"), sync.get("revision"),
    health.get("status"), operation.get("phase"), summary,
    operation_message, len(errors),
))))
'
}

argocd_diagnostics() {
  echo "==> Argo CD diagnostics ($ARGOCD_NS/$APP)" >&2
  # These are read-only calls. Failures are nonfatal during diagnostics.
  local response
  if response="$(oc_get_bounded applications.argoproj.io "$APP" -n "$ARGOCD_NS" -o json 2>&1)"; then
    printf '%s' "$response" | python3 -c '
import json, sys
app=json.load(sys.stdin)
s=app.get("status") or {}
print("Source:", app.get("spec", {}).get("source", {}))
print("Sync:", s.get("sync"))
print("Health:", s.get("health"))
print("Operation phase:", (s.get("operationState") or {}).get("phase"))
print("Operation message:", (s.get("operationState") or {}).get("message"))
for c in s.get("conditions") or []:
    print("Condition:", c.get("type"), c.get("message"))
for r in (s.get("operationState") or {}).get("syncResult", {}).get("resources") or []:
    print("Sync resource:", r.get("kind"), r.get("namespace"), r.get("name"),
          r.get("status"), r.get("message"))
for r in s.get("resources") or []:
    if r.get("status") != "Synced" or r.get("health", {}).get("status") not in (None, "Healthy"):
        print("Resource issue:", r.get("kind"), r.get("namespace"), r.get("name"),
              r.get("status"), r.get("health"))
' >&2 || true
  else
    printf 'Application query failed: %s\n' "$response" >&2
  fi
  echo "==> Namespace workloads (read-only)" >&2
  oc_get_bounded pods,deployments,rollouts.argoproj.io -n "$NS" -o wide >&2 || true
  echo "==> Inspect Argo CD app: oc -n $ARGOCD_NS describe applications.argoproj.io $APP" >&2
}

wait_sync() {
  local revision="$1" snapshot repo sync got health op conditions message error_count
  local end=$((SECONDS + TIMEOUT_SECONDS)) last_state='' state elapsed
  echo "==> Waiting for Argo CD to reconcile ${revision:0:12} (timeout=${TIMEOUT_SECONDS}s; polling=${POLL_SECONDS}s)"
  while (( SECONDS < end )); do
    if ! snapshot="$(argocd_snapshot)"; then
      argocd_diagnostics
      fatal "Cannot read Argo CD sync status; check the API connection and RBAC"
    fi
    IFS=$'\x1f' read -r repo sync got health op conditions message error_count <<< "$snapshot"
    [[ "$repo" == "$EXPECTED_ORIGIN" ]] || {
      argocd_diagnostics
      fatal "Argo CD Application repoURL is ${repo:-missing}, expected $EXPECTED_ORIGIN"
    }
    elapsed=$(( TIMEOUT_SECONDS - (end - SECONDS) ))
    state="sync=${sync:-unknown} health=${health:-unknown} revision=${got:0:12} operation=${op:-none}"
    if [[ "$state" != "$last_state" || $((elapsed % 15)) -lt POLL_SECONDS ]]; then
      printf '    [%ss] %s\n' "$elapsed" "$state"
      [[ -z "$conditions" ]] || printf '    conditions: %.500s\n' "$conditions"
      [[ -z "$message" ]] || printf '    operation message: %.500s\n' "$message"
      last_state="$state"
    fi
    if [[ "$op" == Failed || "$op" == Error || "${error_count:-0}" != 0 ]]; then
      argocd_diagnostics
      fatal "Argo CD reconciliation failed; see Application conditions and sync resources above"
    fi
    if [[ "$sync" == Synced && "$got" == "$revision" ]]; then
      printf 'PASS: Argo CD synchronized requested Git revision %s (health=%s)\n' "${revision:0:12}" "${health:-unknown}"
      return 0
    fi
    sleep "$POLL_SECONDS"
  done
  argocd_diagnostics
  fatal "Argo CD did not sync ${revision:0:12} within ${TIMEOUT_SECONDS}s; see conditions above"
}

wait_rollout() {
  local state="$1" marker="$2" end=$((SECONDS + TIMEOUT_SECONDS))
  local phase spec_marker stable current step color
  while (( SECONDS < end )); do
    phase="$(get_rollout '{.status.phase}')"
    spec_marker="$(get_rollout '{.spec.template.metadata.annotations.demo-rollout-revision}')"
    stable="$(get_rollout '{.status.stableRS}')"
    current="$(get_rollout '{.status.currentPodHash}')"
    step="$(get_rollout '{.status.currentStepIndex}')"
    color="$(get_rollout '{.spec.template.spec.containers[0].env[0].value}')"
    printf '    phase=%s step=%s marker=%s stable=%s current=%s\n' "${phase:-?}" "${step:-?}" "${spec_marker:-?}" "${stable:-?}" "${current:-?}"
    [[ "$phase" != Degraded ]] || fatal "Rollout is Degraded"
    if [[ "$spec_marker" == "$marker" ]]; then
      case "$state" in
        healthy) [[ "$phase" == Healthy && -n "$stable" && "$stable" == "$current" && "$color" == "Application ${INSTANCE^^} - BLUE" ]] && return 0 ;;
        paused) [[ "$phase" == Paused && "$step" == 10 && -n "$current" && "$stable" != "$current" && "$color" == "Application ${INSTANCE^^} - YELLOW" ]] && return 0 ;;
        promoted) [[ "$phase" == Healthy && -n "$stable" && "$stable" == "$current" && "$color" == "Application ${INSTANCE^^} - YELLOW" ]] && return 0 ;;
        *) fatal "Invalid wait_rollout state: $state" ;;
      esac
    fi
    sleep "$POLL_SECONDS"
  done
  fatal "Timed out waiting for ${state} rollout state"
}

spec_info() {
  need python3
  python3 scripts/full-single-service-spec.py verify --instance "$INSTANCE"
}

push_desired() {
  local color="$1" marker="$2"
  python3 scripts/full-single-service-spec.py set --instance "$INSTANCE" --color "$color" --marker "$marker"
  git add "$ROLLOUT_FILE"
  git diff --cached --check
  if ! git diff --cached --quiet; then
    git commit -m "Set full-single-service instance ${INSTANCE} ${color} ${marker}"
    git push origin main
  fi
  git fetch origin main
  [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fatal "Push verification failed"
}

case "$command" in
  validate)
    need python3
    python3 scripts/full-single-service-spec.py verify --instance a
    python3 scripts/full-single-service-spec.py verify --instance b
    echo "Offline validation only: no OpenShift operations."
    ;;
  deploy)
    require_git_head
    spec_info
    require_cluster
    oc apply -f argocd/application-full-single-service.yaml
    # Request a fresh comparison for newly created/reconfigured Applications.
    oc annotate applications.argoproj.io "$APP" -n "$ARGOCD_NS"       argocd.argoproj.io/refresh=hard --overwrite >/dev/null
    wait_sync "$(git rev-parse HEAD)"
    echo "Manifest reconciled. Run: bash scripts/full-single-service.sh prepare"
    ;;
  prepare)
    require_git_head
    spec_info
    require_cluster
    oc get applications.argoproj.io "$APP" -n "$ARGOCD_NS" >/dev/null || fatal "Run deploy first"
    push_desired blue baseline-blue
    wait_sync "$(git rev-parse HEAD)"
    # Trusted recovery: full promotion only for verified BLUE state.
    live_color="$(get_rollout '{.spec.template.spec.containers[0].env[0].value}')"
    live_marker="$(get_rollout '{.spec.template.metadata.annotations.demo-rollout-revision}')"
    [[ "$live_color" == "Application ${INSTANCE^^} - BLUE" && "$live_marker" == baseline-blue ]] ||
      fatal "Expected synchronized BLUE desired state before recovery promotion"
    stable="$(get_rollout '{.status.stableRS}')"
    current="$(get_rollout '{.status.currentPodHash}')"
    phase="$(get_rollout '{.status.phase}')"
    if [[ "$phase" != Healthy || -z "$stable" || "$stable" != "$current" ]]; then
      oc argo rollouts promote "$ROLLOUT" --full -n "$NS"
    fi
    wait_rollout healthy baseline-blue
    echo "Canonical BLUE baseline ready"
    ;;
  start)
    require_git_head
    spec_info
    require_cluster
    [[ "$(get_app '{.spec.source.repoURL}')" == "$EXPECTED_ORIGIN" ]] || fatal "Run deploy first"
    if grep -Fq "              value: \"Application ${INSTANCE^^} - YELLOW\"" "$ROLLOUT_FILE"; then
      wait_sync "$(git rev-parse HEAD)"
      marker="$(get_rollout '{.spec.template.metadata.annotations.demo-rollout-revision}')"
      spec_marker="$(sed -n -E 's/^[[:space:]]*demo-rollout-revision:[[:space:]]*([^[:space:]]+)[[:space:]]*$/\1/p' "$ROLLOUT_FILE" | head -1)"
      [[ "$marker" == "$spec_marker" ]] || fatal "Live and Git candidate markers differ"
      if [[ "$(get_rollout '{.status.phase}')" == Healthy &&
            "$(get_rollout '{.status.stableRS}')" == "$(get_rollout '{.status.currentPodHash}')" ]]; then
        wait_rollout promoted "$spec_marker"
        echo "YELLOW already promoted"
        exit 0
      fi
      wait_rollout paused "$spec_marker"
    else
      wait_sync "$(git rev-parse HEAD)"
      [[ "$(get_rollout '{.status.phase}')" == Healthy ]] || fatal "Prepare BLUE first"
      [[ "$(get_rollout '{.spec.template.spec.containers[0].env[0].value}')" == "Application ${INSTANCE^^} - BLUE" ]] || fatal "Expected BLUE stable"
      [[ "$(get_rollout '{.status.stableRS}')" == "$(get_rollout '{.status.currentPodHash}')" ]] || fatal "BLUE stable/current hashes differ"
      marker="candidate-yellow-$(date -u +%Y%m%dT%H%M%SZ)"
      push_desired yellow "$marker"
      wait_sync "$(git rev-parse HEAD)"
      wait_rollout paused "$marker"
    fi
    echo "YELLOW at final manual pause (Istio 100% candidate); run promote"
    ;;
  promote)
    require_git_head
    spec_info
    require_cluster
    wait_sync "$(git rev-parse HEAD)"
    marker="$(get_rollout '{.spec.template.metadata.annotations.demo-rollout-revision}')"
    expected_marker="$(sed -n -E 's/^[[:space:]]*demo-rollout-revision:[[:space:]]*([^[:space:]]+)[[:space:]]*$/\1/p' "$ROLLOUT_FILE" | head -1)"
    [[ "$marker" == "$expected_marker" && "$marker" == candidate-yellow-* ]] || fatal "Candidate marker mismatch"
    if [[ "$(get_rollout '{.status.phase}')" == Healthy ]]; then
      wait_rollout promoted "$marker"
      echo "YELLOW already promoted"
      exit 0
    fi
    [[ "$(get_rollout '{.status.phase}')" == Paused && "$(get_rollout '{.status.currentStepIndex}')" == 10 ]] || fatal "Not at final promotion gate"
    oc argo rollouts promote "$ROLLOUT" -n "$NS"
    wait_rollout promoted "$marker"
    echo "YELLOW promoted and Healthy"
    ;;
  status)
    require_cluster
    oc get applications.argoproj.io "$APP" -n "$ARGOCD_NS" -o wide
    oc argo rollouts get rollout "$ROLLOUT" -n "$NS"
    oc get virtualservice "$APP-router" -n "$NS" -o yaml
    oc get podmonitor full-single-service-istio-proxies -n "$NS" -o wide
    ;;
  traffic)
    require_cluster
    need curl
    requests="${REQUESTS:-50}"
    [[ "$requests" =~ ^[1-9][0-9]*$ && "$requests" -le 10000 ]] || fatal "REQUESTS must be 1..10000"
    # Both instances use a single public entry point; the header selects A or B.
    host="$(oc get route "$ROUTE" -n "$NS" -o jsonpath='{.spec.host}')"
    [[ -n "$host" ]] || fatal "Route host not found"
    interval="${TRAFFIC_INTERVAL_SECONDS:-0.2}"
    [[ "$interval" =~ ^[0-9]+([.][0-9]+)?$ ]] || fatal "Invalid TRAFFIC_INTERVAL_SECONDS"
    blue=0 yellow=0 errors=0
    app_id="${INSTANCE^^}"
    for ((i=1;i<=requests;i++)); do
      response="$(curl --silent --show-error --fail --insecure --max-time 10 \
        -H "x-app-target: $INSTANCE" "https://${host}/" 2>/dev/null || true)"
      # The greeter returns a single line, including the request count and pod name.
      # Example: Application A - BLUE  greeter => 'pod-name' : 12
      if [[ "$response" =~ ^Application[[:space:]]${app_id}[[:space:]]-[[:space:]](BLUE|YELLOW)[[:space:]].*:[[:space:]]*([0-9]+)$ ]]; then
        case "${BASH_REMATCH[1]}" in
          BLUE) ((blue+=1)) ;;
          YELLOW) ((yellow+=1)) ;;
        esac
        [[ "${SHOW_RESPONSES:-0}" != 1 ]] || printf '%s\n' "$response"
      else
        ((errors+=1))
        echo "    unexpected response: ${response:0:160}" >&2
      fi
      if (( i % 25 == 0 || i == requests )); then
        printf '    instance %s: completed %d/%d (BLUE=%d YELLOW=%d errors=%d)\n' \
          "$INSTANCE" "$i" "$requests" "$blue" "$yellow" "$errors"
      fi
      sleep "$interval"
    done
    printf 'Instance %s: x-app-target=%s via https://%s/ -> BLUE=%d YELLOW=%d errors=%d of %d\n' \
      "$INSTANCE" "$INSTANCE" "$host" "$blue" "$yellow" "$errors" "$requests"
    (( errors == 0 )) || fatal "Some requests failed"
    ;;
  help|-h|--help)
    cat <<'USAGE'
Usage: INSTANCE=a|b bash scripts/full-single-service.sh COMMAND

The default INSTANCE=a operates Rollout A; INSTANCE=b controls B.
Both instances are managed by the same Argo CD Application and share a namespace.

  validate  Validate new files OFFLINE (no cluster commands)
  deploy    Apply ONLY this demo's Argo CD Application; show sync progress and diagnostics
  prepare   Declaratively restore BLUE baseline and recover trusted state
  start     Commit/push YELLOW candidate and wait for final pause
  promote   Approve YELLOW after the last analysis gate
  traffic   Send REQUESTS=50 greeter requests via x-app-target on shared A Route
            TRAFFIC_INTERVAL_SECONDS=0.2 spaces requests; SHOW_RESPONSES=1 prints each counter
  status    Show live Application, Rollout, and Istio VirtualService

Set TIMEOUT_SECONDS=180 for a shorter wait; OC_REQUEST_TIMEOUT=12s caps API reads.
Commands other than validate/help intentionally communicate with OpenShift.
The Git-only MIGRATION scripts are completely independent of this demo.
USAGE
    ;;
  *) fatal "Unknown command '$command' (see help)";;
esac
