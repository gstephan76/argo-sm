#!/usr/bin/env bash
set -Eeuo pipefail

# Read-only: check. Mutation: repair (the live Istio VirtualService ONLY).
# Never deletes routes or changes live stable/canary traffic weights.
MODE="${1:-check}"
case "$MODE" in check|repair) ;; *) echo "Usage: $0 check|repair" >&2; exit 2 ;; esac
NS=full-single-service
VS=full-single-service-router
APP=full-single-service
ARGOCD_NS="${ARGOCD_NAMESPACE:-openshift-gitops}"
EXPECTED_CLUSTER_API="${EXPECTED_CLUSTER_API:-https://api.cluster-bxz2d.dyn.redhatworkshops.io:6443}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail() { echo "ERROR: $*" >&2; exit 1; }
for tool in git oc jq; do command -v "$tool" >/dev/null || fail "Missing $tool"; done

echo '==> OpenShift target and Argo CD revision'
SERVER="$(oc whoami --show-server)"
echo "    API: $SERVER"
oc get applications.argoproj.io "$APP" -n "$ARGOCD_NS" \
  -o json | jq '{sync: .status.sync, health: .status.health, operation: .status.operationState.phase}'
echo '==> Live Rollouts (one desired replica each)'
oc get rollouts.argoproj.io -n "$NS" -o json |
  jq -r '.items[] | select(.metadata.name=="full-single-service-a" or .metadata.name=="full-single-service-b") |
    [.metadata.name,.spec.replicas,.status.phase,.status.updatedReplicas,.status.availableReplicas,.spec.template.spec.containers[0].image] | @tsv'
echo '==> Live public Routes'
oc get routes.route.openshift.io -n "$NS" -o wide
echo '==> Live VirtualService matches and preserved weights'
oc get virtualservice "$VS" -n "$NS" -o json |
  jq '[.spec.http[] | {name, match, route: [.route[] | {host: .destination.host, weight}]}]'

if [[ "$MODE" == check ]]; then
  echo 'CHECK ONLY: no cluster or Git changes performed.'
  exit 0
fi
[[ "$SERVER" == "$EXPECTED_CLUSTER_API" ]] || fail "Wrong cluster; expected $EXPECTED_CLUSTER_API"

# Ensure both selected workloads have been configured with the Greeter and that
# the candidate revision has at least one pod. The new route will not be changed
# by this script; GitOps reconciles it separately.
oc get rollouts.argoproj.io -n "$NS" -o json | jq -e '
  [.items[] | select(.metadata.name=="full-single-service-a" or .metadata.name=="full-single-service-b")] as $r |
  ($r | length)==2 and
  all($r[];
    .spec.replicas==1 and
    .spec.template.spec.containers[0].image=="quay.io/rhdevelopers/knative-tutorial-greeter:quarkus" and
    (.status.updatedReplicas // 0)>=1 and
    (.status.readyReplicas // 0)>=1 and
    .status.phase!="Degraded")
' >/dev/null || fail 'A/B Greeter Rollouts are not ready for routing repair'

git -C "$ROOT" fetch origin main
REMOTE="$(git -C "$ROOT" remote get-url origin)"
case "$REMOTE" in
  https://github.com/gstephan76/argo-sm|https://github.com/gstephan76/argo-sm.git|git@github.com:gstephan76/argo-sm.git) ;;
  *) fail "Unexpected Git origin $REMOTE" ;;
esac
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT

git -C "$ROOT" show origin/main:full-single-service/virtualservice.yaml |
  oc create --dry-run=client --validate=false -f - -o json >"$TMP/desired.json"
oc get virtualservice "$VS" -n "$NS" -o json >"$TMP/live.json"

jq -n --slurpfile live "$TMP/live.json" --slurpfile desired "$TMP/desired.json" '
  ($live[0]) as $l | ($desired[0]) as $d |
  if ($l.metadata.name != "full-single-service-router" or
      $d.metadata.name != "full-single-service-router" or
      $l.metadata.namespace != "full-single-service" or
      $d.metadata.namespace != "full-single-service" or
      ($d.spec.http | length) != 2 or
      # Permit the normal two-route state OR the known duplicated route state.
      # Duplicates must be byte-for-byte equal so no traffic settings are lost.
      (if ($l.spec.http | length) == 2 then
         ([$l.spec.http[].name] | sort) != ["primary", "secondary"]
       elif ($l.spec.http | length) == 4 then
         ([$l.spec.http[].name] != ["secondary", "secondary", "primary", "primary"] or
          $l.spec.http[0] != $l.spec.http[1] or
          $l.spec.http[2] != $l.spec.http[3])
       else true end) or
      [$d.spec.http[].name] != ["secondary", "primary"] or
      $d.spec.http[0].match[0].headers."x-app-target".exact != "b" or
      $d.spec.http[1].match[0].uri.prefix != "/")
  then error("Refusing repair: unexpected Istio route structure or desired header match")
  else
    ($d.spec.http | map(
      . as $wanted |
      ($l.spec.http | map(select(.name == $wanted.name)) | .[0]) as $old |
      if ($old.route | length)!=2 or ($wanted.route | length)!=2 or
         ([$old.route[].destination.host] != [$wanted.route[].destination.host]) or
         (($old.route[0].weight + $old.route[1].weight)!=100) or
         any($old.route[]; (.weight | type) != "number")
      then error("Refusing repair: live Rollouts route destination or weight drift")
      else ($wanted |
        .route[0].weight = $old.route[0].weight |
        .route[1].weight = $old.route[1].weight)
      end
    )) as $next |
    if ([$next[].name] != ["secondary", "primary"] or ($next | length) != 2)
    then error("Refusing repair: proposed VirtualService must contain exactly two ordered routes")
    elif $l.spec.http == $next then []
    else [
      {op:"test",path:"/metadata/resourceVersion",value:$l.metadata.resourceVersion},
      {op:"replace",path:"/spec/http",value:$next}
    ] end
  end
' >"$TMP/patch.json"

if [[ "$(jq length "$TMP/patch.json")" == 0 ]]; then
  echo 'PASS: live VirtualService already matches Git header routing; no patch needed.'
  exit 0
fi

echo '==> Preserving live per-application canary weights; repairing match/order only:'
jq '.[] | select(.op == "replace") | .value | map({name, match, weights: [.route[].weight]})' "$TMP/patch.json"
oc patch virtualservice "$VS" -n "$NS" --type=json --patch-file="$TMP/patch.json"
echo 'PASS: VirtualService match/order corrected; run check and test A/B over the neutral public Route.'
