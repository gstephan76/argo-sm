#!/usr/bin/env bash
# Read-only HTTP verification while CRUD Blue/Green revisions coexist.
set -Eeuo pipefail
NS=full-multi
for command in oc curl jq; do
  command -v "$command" >/dev/null || { echo "Missing: $command" >&2; exit 1; }
done
[[ "${1:-}" == "--preview" && $# -eq 1 ]] || {
  echo 'Usage: bash scripts/full-multi-validate-data-paths.sh --preview' >&2
  echo 'Run after start, when both CRUD Blue/Green pods are Ready.' >&2
  exit 2
}
HOST="$(oc -n "$NS" get route full-multi -o jsonpath='{.spec.host}')"
[[ -n "$HOST" ]] || { echo 'Route/full-multi has no hostname' >&2; exit 1; }
for app in a b; do
  for track in blue green; do
    expected_host="mariadb-${app}-${track}.full-multi.svc.cluster.local"
    case "$track" in
      blue) title='Pride and Prejudice';;
      green) title='Memórias Póstumas de Brás Cubas';;
    esac
    body="$(curl -kfsS --connect-timeout 5 --max-time 20           -H "x-app-target: $app"           -H 'x-front-track: stable' -H 'x-backend-track: stable'           -H "x-db-track: $track" "https://${HOST}/api/books")" || exit 1
    jq -e --arg app "${app^^}" --arg host "$expected_host"           --arg track "$track" --arg title "$title"           '.application==$app and .database_host==$host and .database_track==$track and .books[0].title==$title and (.books|length)>=20'           <<<"$body" >/dev/null || {
        echo "FAIL app=$app CRUD=$track expected DB=$expected_host title=$title" >&2
        jq '{application,database_track,database_host,first_title:(.books[0].title),num_books:(.books|length)}' <<<"$body" >&2
        exit 1
      }
    echo "PASS app=$app CRUD=$track database=$expected_host title=$title"
  done
done
