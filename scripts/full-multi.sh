#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)";cd "$ROOT"
INSTANCE="${INSTANCE:-a}"
[[ "$INSTANCE" == a || "$INSTANCE" == b ]] || { echo 'INSTANCE must be a or b' >&2; exit 1; }
NS=full-multi; APP=full-multi; ARGOCD_NS="${ARGOCD_NAMESPACE:-openshift-gitops}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"; POLL_SECONDS="${POLL_SECONDS:-5}"
# Front/Backend Canary: step 6 pauses at 50%; CRUD BlueGreen has no canary step.
CANARY_PAUSE_STEP_INDEX=6
cmd="${1:-help}"
fail(){ echo "ERROR: $*" >&2; exit 1; }
require(){ command -v "$1" >/dev/null || fail "Missing $1"; }
check_git(){
 require git
 [[ "$(git branch --show-current)" == main ]] || fail 'Must be on main'
 git diff --quiet && git diff --cached --quiet || fail 'Commit tracked changes first'
 git fetch origin main
 [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail 'HEAD must match origin/main'
 [[ "$(git remote get-url origin)" == *gstephan76/argo-sm* ]] || fail 'Unexpected origin'
}
ready(){
 local name="$1" desired="$2" start=$SECONDS phase step previous=''
 while (( SECONDS-start < TIMEOUT_SECONDS )); do
   phase="$(oc get rollout "$name" -n "$NS" -o jsonpath='{.status.phase}')"
   step="$(oc get rollout "$name" -n "$NS" -o jsonpath='{.status.currentStepIndex}')"
   if [[ "$phase/$step" != "$previous" ]]; then
     echo "ROLLOUT $name phase=${phase:-Unreconciled} step=${step:-none} desired=$desired"
     previous="$phase/$step"
   fi
   if [[ "$phase" == Healthy && "$desired" == healthy ]];then
     if [[ "$cmd" == prepare ]];then
       marker="$(oc -n "$NS" get rollout "$name" -o jsonpath='{.spec.template.metadata.annotations.demo-rollout-revision}')"
       generation="$(oc -n "$NS" get rollout "$name" -o jsonpath='{.metadata.generation}')"
       observed="$(oc -n "$NS" get rollout "$name" -o jsonpath='{.status.observedGeneration}')"
       [[ "$marker" == baseline-blue ]] || fail "$name: expected Blue baseline, got $marker"
       if [[ "$observed" != "$generation" ]];then sleep "$POLL_SECONDS";continue;fi
     fi
     return 0
   fi
   if [[ "$phase" == Paused ]];then
     if [[ "$desired" == paused ]];then
       # HTTP canaries must reach 50%; CRUD pauses without a timer.
       if [[ "$name" == crud-* || "$step" == "$CANARY_PAUSE_STEP_INDEX" ]];then return 0;fi
     elif [[ "$desired" == healthy && "$cmd" == prepare ]];then
       # An explicit PREPARE operation may rebaseline Blue; never promote a
       # YELLOW/GREEN candidate under the guise of returning to health.
       marker="$(oc -n "$NS" get rollout "$name" -o jsonpath='{.spec.template.metadata.annotations.demo-rollout-revision}')"
       [[ "$marker" == baseline-blue ]] || fail "$name: refuse to promote non-Blue marker=$marker"
       oc argo rollouts promote "$name" -n "$NS"
       continue
     fi
   fi
   [[ "$phase" != Degraded ]] || fail "$name degraded"
   sleep "$POLL_SECONDS"
 done
 fail "timeout waiting for $name desired=$desired"
}
sync_git(){
 local wanted
 wanted="$(git rev-parse HEAD)"
 oc annotate application "$APP" -n "$ARGOCD_NS" argocd.argoproj.io/refresh=hard --overwrite >/dev/null
 # Old MariaDB Rollouts are deliberately retained with prune=false.
   # In that protected state revision observation + real workload gates are
   # required; an unconditional Synced check may never succeed.
   pruning="$(oc -n "$ARGOCD_NS" get application "$APP" -o jsonpath='{.spec.syncPolicy.automated.prune}')"
   if [[ "$pruning" == false ]];then
     python3 scripts/full-multi-wait-gitops-revision.py --revision "$wanted" \
       --timeout "$TIMEOUT_SECONDS" --poll "$POLL_SECONDS"
   else
     python3 scripts/full-multi-bootstrap.py wait-sync \
       --application "$APP" --argocd-namespace "$ARGOCD_NS" \
       --revision "$wanted" --timeout "$TIMEOUT_SECONDS" --poll "$POLL_SECONDS"
   fi
}
rollouts_cli(){
 oc argo rollouts version >/dev/null 2>&1 || \
   fail 'Missing oc argo rollouts CLI plugin; install kubectl-argo-rollouts on this client'
}
commit_stage(){
 local color="$1"
 python3 scripts/full-multi-spec.py set --instance "$INSTANCE" --color "$color"
 git add full-multi/rollout-front-"$INSTANCE".yaml full-multi/rollout-backend-"$INSTANCE".yaml full-multi/rollout-crud-"$INSTANCE".yaml
 git diff --cached --check
 if ! git diff --cached --quiet;then
   git commit -m "Set full-multi instance $INSTANCE $color"
   git push origin main
 fi
}
case "$cmd" in
 validate)
   python3 scripts/full-multi-spec.py verify
   ;;
 deploy)
   check_git
   python3 scripts/full-multi-spec.py verify
   require oc
   oc apply -f full-multi/namespace.yaml
   python3 scripts/full-multi-bootstrap.py preflight \
     --namespace "$NS" --argocd-namespace "$ARGOCD_NS"
   oc apply -f argocd/application-full-multi.yaml
   sync_git
   python3 scripts/full-multi-bootstrap.py wait-ready \
     --namespace "$NS" --timeout "$TIMEOUT_SECONDS" --poll "$POLL_SECONDS"
   echo 'PASS: full-multi GitOps synchronized and workloads ready; HTTP/Tempo acceptance remains separate'
   ;;
 prepare|start)
   check_git
   python3 scripts/full-multi-spec.py verify
   require oc
   rollouts_cli
   python3 scripts/full-multi-bootstrap.py preflight \
     --namespace "$NS" --argocd-namespace "$ARGOCD_NS"
   oc get application "$APP" -n "$ARGOCD_NS" >/dev/null || fail 'Run deploy first'
   if [[ "$cmd" == prepare ]];then
     # Refuse to overwrite the deliberately paused GREEN demo preview. A
     # technical BLUE-to-BLUE image update (such as CRUD v5) is allowed here.
     phase="$(oc -n "$NS" get rollout "crud-$INSTANCE" -o jsonpath='{.status.phase}')"
     if [[ "$phase" == Paused ]];then
       track="$(oc -n "$NS" get rollout "crud-$INSTANCE" -o jsonpath='{.spec.template.metadata.labels.demo\.crud-track}')"
       [[ "$track" == blue ]] || fail "crud-$INSTANCE has a paused GREEN preview; promote it explicitly after the demo before preparing"
     fi
     python3 scripts/full-multi-history-prune.py --instance "$INSTANCE"
     commit_stage blue
   else
     # Do not replace an existing preview or silently promote BlueGreen.
     phase="$(oc -n "$NS" get rollout "crud-$INSTANCE" -o jsonpath='{.status.phase}')"
     [[ "$phase" == Healthy ]] || fail "crud-$INSTANCE must be Healthy/Blue before start (phase=$phase)"
     color="$(oc -n "$NS" get rollout "crud-$INSTANCE" -o jsonpath='{.spec.template.metadata.labels.demo\.crud-track}')"
     [[ "$color" == blue ]] || fail "crud-$INSTANCE must have Blue baseline before start (track=$color)"
     # Read-only MariaDB grants are mandatory before opening Green preview.
     bash scripts/full-multi-db-readonly.sh --verify
     commit_stage yellow
   fi
   sync_git
   for tier in crud backend front;do ready "$tier-$INSTANCE" "$([[ "$cmd" == prepare ]] && echo healthy || echo paused)";done
   if [[ "$cmd" == start ]];then
     python3 scripts/full-multi-crud-pause-gate.py --instance "$INSTANCE"
     # HTTP + SQL verification for this application's two independent paths.
     bash scripts/full-multi-validate-data-paths.sh --instance "$INSTANCE"
   fi
   if [[ "$cmd" == prepare ]];then
     python3 scripts/full-multi-history-reset.py --instance "$INSTANCE" --timeout-seconds "$TIMEOUT_SECONDS"
     echo "CRUD history pruning is included in the selected lifecycle tiers"
     echo "PASS: Full-multi $INSTANCE BLUE prepared; history reset"
   else
     echo "PASS: Full-multi $INSTANCE Front/Backend paused at 50%; CRUD Blue active and Green preview BOTH ready and Paused indefinitely. Promote only by explicit command."
   fi
   ;;
 promote)
   require oc
   rollouts_cli
   python3 scripts/full-multi-bootstrap.py preflight \
     --namespace "$NS" --argocd-namespace "$ARGOCD_NS"
   # GREEN preview promotion requires strict concurrent Blue/Green evidence.
   # A pre-demo BLUE-to-BLUE image upgrade remains explicitly promotable too.
   phase="$(oc -n "$NS" get rollout "crud-$INSTANCE" -o jsonpath='{.status.phase}')"
   if [[ "$phase" == Paused ]];then
     track="$(oc -n "$NS" get rollout "crud-$INSTANCE" -o jsonpath='{.spec.template.metadata.labels.demo\.crud-track}')"
     if [[ "$track" == green ]];then
       python3 scripts/full-multi-crud-pause-gate.py --instance "$INSTANCE"
     else
       [[ "$track" == blue ]] || fail "crud-$INSTANCE unexpected paused track=$track"
       echo "Promoting a pre-demo BLUE revision upgrade (no GREEN preview yet)"
     fi
   fi
   for tier in crud backend front;do
     name="$tier-$INSTANCE"
     phase="$(oc get rollout "$name" -n "$NS" -o jsonpath='{.status.phase}')"
     if [[ "$phase" == Paused ]];then
       if [[ "$name" != crud-* ]];then
         step="$(oc get rollout "$name" -n "$NS" -o jsonpath='{.status.currentStepIndex}')"
         [[ "$step" == "$CANARY_PAUSE_STEP_INDEX" ]] || fail "$name paused at step=$step; expected 50% pause step=$CANARY_PAUSE_STEP_INDEX"
       fi
       oc argo rollouts promote "$name" -n "$NS"
     fi
     ready "$name" healthy
   done
   echo "PASS: Full-multi $INSTANCE promoted"
   ;;
 cleanup)
   require oc
   python3 scripts/full-multi-history-prune.py --instance "$INSTANCE" "${@:2}"
   ;;
 status)
   oc get rollouts.argoproj.io -n "$NS"
   oc get svc -n "$NS"
   oc get virtualservices -n "$NS"
   oc get route full-multi -n "$NS"
   oc -n "$ARGOCD_NS" get application "$APP" \
     -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,REVISION:.status.sync.revision'
   oc get rolloutmanager -A
   echo 'CRUD paused safety gate (reports errors only when a rollout is paused):'
   for app in a b;do
     phase="$(oc -n "$NS" get rollout "crud-$app" -o jsonpath='{.status.phase}')"
     if [[ "$phase" == Paused ]];then python3 scripts/full-multi-crud-pause-gate.py --instance "$app";fi
   done
   ;;
 traffic)
   require curl
   host="$(oc get route full-multi -n "$NS" -o jsonpath='{.spec.host}')"
   front_track="${FRONT_TRACK:-stable}"
   backend_track="${BACKEND_TRACK:-stable}"
   db_track="${DB_TRACK:-active}"
   [[ "$front_track" =~ ^(stable|canary|weighted)$ ]] || fail 'FRONT_TRACK must be stable|canary|weighted'
   [[ "$backend_track" =~ ^(stable|canary|weighted)$ ]] || fail 'BACKEND_TRACK must be stable|canary|weighted'
   [[ "$db_track" =~ ^(stable|canary|blue|green|active)$ ]] || fail 'DB_TRACK must be stable|canary|blue|green|active'
   headers=(-H "x-app-target: $INSTANCE")
   [[ "$db_track" == active ]] || headers+=(-H "x-db-track: $db_track")
   [[ "$front_track" == weighted ]] || headers+=(-H "x-front-track: $front_track")
   [[ "$backend_track" == weighted ]] || headers+=(-H "x-backend-track: $backend_track")
   for ((i=1;i<=${REQUESTS:-30};i++));do
     curl -kfsS -o /dev/null -w "request $i: HTTP %{http_code}\n" \
       "${headers[@]}" "https://$host/?page=1&per_page=3"
   done
   ;;
 help|-h|--help)
   echo 'Usage: INSTANCE=a|b bash scripts/full-multi.sh validate|deploy|prepare|start|promote|cleanup|traffic|status'
   echo 'Set FRONT_TRACK/BACKEND_TRACK=stable|canary|weighted and DB_TRACK=active|blue|green (or stable|canary).'
   echo 'weighted omits that tier header and lets Istio enforce 50/50 at the canary pause.'
   ;;
 *)fail "Unknown command $cmd";;
esac
