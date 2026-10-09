#!/usr/bin/env bash
set -Eeuo pipefail

# Explicit demo commands only. Never used by the Git-only migration workflow.
NS="full-single-service"
APP="full-single-service"
ROLLOUT="full-single-service"
ARGOCD_NS="${ARGOCD_NAMESPACE:-openshift-gitops}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-600}"
POLL_SECONDS="${POLL_SECONDS:-5}"
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

get_rollout() {
  oc get rollouts.argoproj.io "$ROLLOUT" -n "$NS" -o "jsonpath=$1" 2>/dev/null || true
}

get_app() {
  oc get applications.argoproj.io "$APP" -n "$ARGOCD_NS" -o "jsonpath=$1" 2>/dev/null || true
}

wait_sync() {
  local revision="$1" sync got op repo end=$((SECONDS + TIMEOUT_SECONDS))
  echo "==> Waiting for Argo CD to reconcile ${revision:0:12}"
  while (( SECONDS < end )); do
    repo="$(get_app '{.spec.source.repoURL}')"
    [[ "$repo" == "$EXPECTED_ORIGIN" ]] || fatal "Argo CD Application repoURL is ${repo:-missing}"
    sync="$(get_app '{.status.sync.status}')"
    got="$(get_app '{.status.sync.revision}')"
    op="$(get_app '{.status.operationState.phase}')"
    [[ "$op" != Failed && "$op" != Error ]] || fatal "Argo CD operation failed"
    [[ "$sync" == Synced && "$got" == "$revision" ]] && return 0
    sleep "$POLL_SECONDS"
  done
  fatal "Argo CD did not sync ${revision:0:12} within ${TIMEOUT_SECONDS}s"
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
    color="$(get_rollout '{.spec.template.spec.containers[0].image}')"
    printf '    phase=%s step=%s marker=%s stable=%s current=%s\n' "${phase:-?}" "${step:-?}" "${spec_marker:-?}" "${stable:-?}" "${current:-?}"
    [[ "$phase" != Degraded ]] || fatal "Rollout is Degraded"
    if [[ "$spec_marker" == "$marker" ]]; then
      case "$state" in
        healthy) [[ "$phase" == Healthy && -n "$stable" && "$stable" == "$current" && "$color" == argoproj/rollouts-demo:blue ]] && return 0 ;;
        paused) [[ "$phase" == Paused && "$step" == 10 && -n "$current" && "$stable" != "$current" && "$color" == argoproj/rollouts-demo:green ]] && return 0 ;;
        promoted) [[ "$phase" == Healthy && -n "$stable" && "$stable" == "$current" && "$color" == argoproj/rollouts-demo:green ]] && return 0 ;;
        *) fatal "Invalid wait_rollout state: $state" ;;
      esac
    fi
    sleep "$POLL_SECONDS"
  done
  fatal "Timed out waiting for ${state} rollout state"
}

spec_info() {
  need python3
  python3 scripts/full-single-service-spec.py verify
}

push_desired() {
  local color="$1" marker="$2"
  python3 scripts/full-single-service-spec.py set --color "$color" --marker "$marker"
  git add full-single-service/rollout.yaml
  git diff --cached --check
  if ! git diff --cached --quiet; then
    git commit -m "Set full-single-service ${color} ${marker}"
    git push origin main
  fi
  git fetch origin main
  [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fatal "Push verification failed"
}

case "$command" in
  validate)
    spec_info
    echo "Offline validation only: no OpenShift operations."
    ;;
  deploy)
    require_git_head
    spec_info
    require_cluster
    oc apply -f argocd/application-full-single-service.yaml
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
    # Trusted recovery: full promotion is requested only for the verified BLUE spec.
    live_image="$(get_rollout '{.spec.template.spec.containers[0].image}')"
    live_marker="$(get_rollout '{.spec.template.metadata.annotations.demo-rollout-revision}')"
    [[ "$live_image" == argoproj/rollouts-demo:blue && "$live_marker" == baseline-blue ]] ||
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
    if grep -Eq 'image: argoproj/rollouts-demo:green$' full-single-service/rollout.yaml; then
      wait_sync "$(git rev-parse HEAD)"
      marker="$(get_rollout '{.spec.template.metadata.annotations.demo-rollout-revision}')"
      spec_marker="$(python3 -c 'import re,pathlib; print(re.search(r"demo-rollout-revision: ([^\s]+)", pathlib.Path("full-single-service/rollout.yaml").read_text()).group(1))')"
      [[ "$marker" == "$spec_marker" ]] || fatal "Live and Git candidate markers differ"
      if [[ "$(get_rollout '{.status.phase}')" == Healthy &&
            "$(get_rollout '{.status.stableRS}')" == "$(get_rollout '{.status.currentPodHash}')" ]]; then
        wait_rollout promoted "$spec_marker"
        echo "GREEN already promoted"
        exit 0
      fi
      wait_rollout paused "$spec_marker"
    else
      wait_sync "$(git rev-parse HEAD)"
      [[ "$(get_rollout '{.status.phase}')" == Healthy ]] || fatal "Prepare BLUE first"
      [[ "$(get_rollout '{.spec.template.spec.containers[0].image}')" == argoproj/rollouts-demo:blue ]] || fatal "Expected BLUE stable"
      [[ "$(get_rollout '{.status.stableRS}')" == "$(get_rollout '{.status.currentPodHash}')" ]] || fatal "BLUE stable/current hashes differ"
      marker="candidate-green-$(date -u +%Y%m%dT%H%M%SZ)"
      push_desired green "$marker"
      wait_sync "$(git rev-parse HEAD)"
      wait_rollout paused "$marker"
    fi
    echo "GREEN at final manual pause (Istio 100% candidate); run promote"
    ;;
  promote)
    require_git_head
    spec_info
    require_cluster
    wait_sync "$(git rev-parse HEAD)"
    marker="$(get_rollout '{.spec.template.metadata.annotations.demo-rollout-revision}')"
    expected_marker="$(python3 -c 'import re,pathlib; print(re.search(r"demo-rollout-revision: ([^\s]+)", pathlib.Path("full-single-service/rollout.yaml").read_text()).group(1))')"
    [[ "$marker" == "$expected_marker" && "$marker" == candidate-green-* ]] || fatal "Candidate marker mismatch"
    if [[ "$(get_rollout '{.status.phase}')" == Healthy ]]; then
      wait_rollout promoted "$marker"
      echo "GREEN already promoted"
      exit 0
    fi
    [[ "$(get_rollout '{.status.phase}')" == Paused && "$(get_rollout '{.status.currentStepIndex}')" == 10 ]] || fatal "Not at final promotion gate"
    oc argo rollouts promote "$ROLLOUT" -n "$NS"
    wait_rollout promoted "$marker"
    echo "GREEN promoted and Healthy"
    ;;
  status)
    require_cluster
    oc get applications.argoproj.io "$APP" -n "$ARGOCD_NS" -o wide
    oc argo rollouts get rollout "$ROLLOUT" -n "$NS"
    oc get virtualservice "$APP-router" -n "$NS" -o yaml
    ;;
  traffic)
    require_cluster
    need curl
    requests="${REQUESTS:-50}"
    [[ "$requests" =~ ^[1-9][0-9]*$ && "$requests" -le 10000 ]] || fatal "REQUESTS must be 1..10000"
    host="$(oc get route "$APP" -n "$NS" -o jsonpath='{.spec.host}')"
    [[ -n "$host" ]] || fatal "Route host not found"
    blue=0 green=0 errors=0
    for ((i=1;i<=requests;i++)); do
      color="$(curl --silent --show-error --fail --insecure --max-time 10 "https://${host}/color" 2>/dev/null || true)"
      case "$color" in
        '"blue"') ((blue+=1));;
        '"green"') ((green+=1));;
        *) ((errors+=1));;
      esac
    done
    printf 'Route https://%s/color -> blue=%d green=%d errors=%d of %d\n' "$host" "$blue" "$green" "$errors" "$requests"
    (( errors == 0 )) || fatal "Some requests failed"
    ;;
  help|-h|--help)
    cat <<'USAGE'
Usage: bash scripts/full-single-service.sh COMMAND

  validate  Validate new files OFFLINE (no cluster commands)
  deploy    Apply ONLY this demo's Argo CD Application; wait for sync
  prepare   Declaratively reset to BLUE and recover trusted baseline
  start     Commit/push GREEN candidate; wait for 10/25/50/75/100 canary and final pause
  promote   Manually approve GREEN after the last analysis gate
  traffic   Send REQUESTS=50 traffic samples through Route and count colors
  status    Show live Application, Rollout, and Istio VirtualService

Commands other than validate/help intentionally communicate with OpenShift.
The Git-only MIGRATION scripts are completely independent of this demo.
USAGE
    ;;
  *) fatal "Unknown command '$command' (see help)";;
esac
