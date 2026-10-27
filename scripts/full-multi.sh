#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)";cd "$ROOT"
INSTANCE="${INSTANCE:-a}"
[[ "$INSTANCE" == a || "$INSTANCE" == b ]] || { echo 'INSTANCE must be a or b' >&2; exit 1; }
NS=full-multi; APP=full-multi; ARGOCD_NS="${ARGOCD_NAMESPACE:-openshift-gitops}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"; POLL_SECONDS="${POLL_SECONDS:-5}"
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
 local name="$1" desired="$2" start=$SECONDS phase step
 while (( SECONDS-start < TIMEOUT_SECONDS )); do
   phase="$(oc get rollout "$name" -n "$NS" -o jsonpath='{.status.phase}')"
   step="$(oc get rollout "$name" -n "$NS" -o jsonpath='{.status.currentStepIndex}')"
   if [[ "$phase" == Healthy ]]; then [[ "$desired" == healthy ]] && return 0;fi
   if [[ "$phase" == Paused ]]; then
     if [[ "$desired" == paused ]]; then
       # Only HTTP Rollouts should wait for the final 100% analysis gate.
       if [[ "$name" == mariadb-* || "$step" == 10 ]]; then return 0;fi
     elif [[ "$desired" == healthy && ( "$name" == mariadb-* || "$step" == 10 ) ]]; then
       oc argo rollouts promote "$name" -n "$NS";continue
     fi
   fi
   [[ "$phase" != Degraded ]] || fail "$name degraded"
   sleep "$POLL_SECONDS"
 done
 fail "timeout waiting for $name desired=$desired"
}
sync_git(){
 local wanted="$(git rev-parse HEAD)" start=$SECONDS got st
 oc annotate application "$APP" -n "$ARGOCD_NS" argocd.argoproj.io/refresh=hard --overwrite >/dev/null
 while (( SECONDS-start < TIMEOUT_SECONDS )); do
   got="$(oc get application "$APP" -n "$ARGOCD_NS" -o jsonpath='{.status.sync.revision}')"
   st="$(oc get application "$APP" -n "$ARGOCD_NS" -o jsonpath='{.status.sync.status}')"
   if [[ "$got" == "$wanted" && "$st" == Synced ]];then return 0;fi
   sleep "$POLL_SECONDS"
 done
 fail "Argo CD did not sync HEAD ${wanted:0:12}"
}
commit_stage(){
 local color="$1"
 python3 scripts/full-multi-spec.py set --instance "$INSTANCE" --color "$color"
 git add full-multi/rollout-front-"$INSTANCE".yaml full-multi/rollout-backend-"$INSTANCE".yaml full-multi/rollout-mariadb-"$INSTANCE".yaml
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
   for app in a b;do oc get secret "db-credentials-$app" -n "$NS" >/dev/null || fail "Run scripts/full-multi-secrets.sh before deploy";done
   oc apply -f argocd/application-full-multi.yaml
   sync_git
   echo 'PASS: full-multi GitOps synced; verify readiness with status'
   ;;
 prepare|start)
   check_git
   python3 scripts/full-multi-spec.py verify
   require oc
   oc get application "$APP" -n "$ARGOCD_NS" >/dev/null || fail 'Run deploy first'
   if [[ "$cmd" == prepare ]];then commit_stage blue;else commit_stage yellow;fi
   sync_git
   for tier in mariadb backend front;do ready "$tier-$INSTANCE" "$([[ "$cmd" == prepare ]] && echo healthy || echo paused)";done
   if [[ "$cmd" == prepare ]];then
     python3 scripts/full-multi-history-reset.py --instance "$INSTANCE" --timeout-seconds "$TIMEOUT_SECONDS"
     echo "PASS: Full-multi $INSTANCE BLUE prepared; history reset"
   else
     echo "PASS: Full-multi $INSTANCE canaries at manual pause (database preview ready); run promote"
   fi
   ;;
 promote)
   require oc
   for tier in mariadb backend front;do
     name="$tier-$INSTANCE"
     phase="$(oc get rollout "$name" -n "$NS" -o jsonpath='{.status.phase}')"
     if [[ "$phase" == Paused ]];then oc argo rollouts promote "$name" -n "$NS";fi
     ready "$name" healthy
   done
   echo "PASS: Full-multi $INSTANCE promoted"
   ;;
 status)
   oc get rollouts.argoproj.io -n "$NS"
   oc get svc -n "$NS"
   oc get virtualservices -n "$NS"
   oc get route full-multi -n "$NS"
   ;;
 traffic)
   require curl
   host="$(oc get route full-multi -n "$NS" -o jsonpath='{.spec.host}')"
   for ((i=1;i<=${REQUESTS:-30};i++));do
     curl -kfsS -o /dev/null -w "request $i: HTTP %{http_code}\n" \
       -H "x-app-target: $INSTANCE" \
       -H "x-front-track: ${FRONT_TRACK:-stable}" \
       -H "x-backend-track: ${BACKEND_TRACK:-stable}" \
       -H "x-db-track: ${DB_TRACK:-stable}" "https://$host/?page=1&per_page=3"
   done
   ;;
 help|-h|--help)
   echo 'Usage: INSTANCE=a|b bash scripts/full-multi.sh validate|deploy|prepare|start|promote|traffic|status'
   echo 'Set FRONT_TRACK, BACKEND_TRACK, DB_TRACK=stable|canary to route the full path during traffic.'
   ;;
 *)fail "Unknown command $cmd";;
esac
