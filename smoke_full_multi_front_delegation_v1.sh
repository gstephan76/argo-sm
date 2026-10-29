#!/usr/bin/env bash
# Validate live post-activation delegated ingress, 17 Services and all 16 A/B/DB paths.
set -Eeuo pipefail
for tool in oc jq curl; do command -v "$tool" >/dev/null || { echo "Missing $tool" >&2; exit 1; }; done
NS=full-multi
HOST="$(oc -n "$NS" get route full-multi -o jsonpath='{.spec.host}')"
[[ -n "$HOST" ]] || { echo 'Missing Route host' >&2; exit 1; }
SVC_COUNT="$(oc -n "$NS" get svc -o name | wc -l | tr -d ' ')"
[[ "$SVC_COUNT" == 17 ]] || { echo "ERROR: expected 17 Services; got $SVC_COUNT" >&2; exit 1; }

oc -n "$NS" get virtualservice full-multi-front-router -o json | jq -e '
  .spec.hosts == ["*"] and
  .spec.gateways == ["full-multi-gateway"] and
  (.spec.http|length)==2 and
  .spec.http[0].name == "application-b" and
  .spec.http[0].match[0].headers["x-app-target"].exact == "b" and
  .spec.http[0].delegate.name == "front-b-router" and
  .spec.http[1].name == "application-a-default" and
  .spec.http[1].delegate.name == "front-a-router" and
  (.spec.http | all(has("route")|not))
' >/dev/null || { echo 'Root delegation topology invalid' >&2; exit 1; }

for app in a b; do
  route=secondary; [[ "$app" == a ]] && route=primary
  oc -n "$NS" get virtualservice "front-$app-router" -o json | jq -e --arg route "$route" --arg app "$app" '
    (.spec | has("hosts") | not) and
    (.spec | has("gateways") | not) and
    ([.spec.http[].name]|index($route)!=null) and
    ([.spec.http[].name]|index("front-"+$app+"-force-stable")!=null) and
    ([.spec.http[].name]|index("front-"+$app+"-force-canary")!=null) and
    ([.spec.http[]|select(.name==$route)|.route[].weight]|add)==100
  ' >/dev/null || { echo "Invalid front-$app-router delegate" >&2; exit 1; }
  oc -n "$NS" get rollouts.argoproj.io "front-$app" -o json | jq -e --arg app "$app" --arg route "$route" '
    .spec.strategy.canary.trafficRouting.istio.virtualService.name == ("front-"+$app+"-router") and
    .spec.strategy.canary.trafficRouting.istio.virtualService.routes == [$route]
  ' >/dev/null || { echo "Frontend Rollout $app not delegated" >&2; exit 1; }
done

echo "PASS: root and delegates active, both Frontend Rollouts retargeted, 17 Services"
count=0
for app in a b; do
  for ft in stable canary; do
    for bt in stable canary; do
      for db in blue green; do
        body="$(curl -kfsS --connect-timeout 5 --max-time 20 \
          -H "x-app-target: $app" \
          -H "x-front-track: $ft" \
          -H "x-backend-track: $bt" \
          -H "x-db-track: $db" \
          "https://$HOST/api/books")" || { echo "HTTP failed: $app/$ft/$bt/$db" >&2; exit 1; }
        jq -e --arg app "$app" --arg ft "$ft" --arg bt "$bt" --arg db "$db" '
          (.application == ($app|ascii_upcase)) and
          (.frontend_release == (if $ft=="stable" then "BLUE" else "YELLOW" end)) and
          (.backend_release == (if $bt=="stable" then "BLUE" else "YELLOW" end)) and
          (.database_track == (if $db=="blue" then "stable" else "canary" end)) and
          (.database_host == ("mariadb-"+$app+"-"+(if $db=="blue" then "stable" else "canary" end)+".full-multi.svc.cluster.local")) and
          ((.books|length)==20) and
          (.books[0].title == (if $db=="blue" then "Pride and Prejudice" else "Memórias Póstumas de Brás Cubas" end))
        ' <<<"$body" >/dev/null || { echo "Unexpected JSON for $app/$ft/$bt/$db: $body" >&2; exit 1; }
        printf 'PASS %-1s front=%-6s backend=%-6s db=%-5s first_book=%s\n' "$app" "$ft" "$bt" "$db" \
          "$(jq -r '.books[0].title' <<<"$body")"
        ((count+=1))
      done
    done
  done
done
echo "PASS: all $count forced routing combinations verified"
