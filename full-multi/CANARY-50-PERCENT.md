# full-multi: 50% hold-and-promote workflow

This workflow follows the two-lane A/B topology: four HTTP Rollouts
(`front-a`, `backend-a`, `front-b`, `backend-b`) independently advance from
BLUE stable to YELLOW candidate, pause at **50% Istio traffic weight**, and
wait for an explicit promotion. `mariadb-a` and `mariadb-b` remain
**blue-green** with their own active and preview TCP Services.

## Four HTTP canaries

Each HTTP Rollout uses the following steps (0-based pause index **6**):

```yaml
steps:
  - setWeight: 10
  - analysis: # existing tier-specific smoke AnalysisTemplate
      templates: [{templateName: smoke-front-a}]
      args: [{name: expected-color, value: YELLOW}]
  - setWeight: 25
  - analysis: # same existing template and expected color
      templates: [{templateName: smoke-front-a}]
      args: [{name: expected-color, value: YELLOW}]
  - setWeight: 50
  - analysis: # same existing template and expected color
      templates: [{templateName: smoke-front-a}]
      args: [{name: expected-color, value: YELLOW}]
  - pause: {}  # indefinite until manual promote
```

The example illustrates the Front A analysis template. Actual manifests use
the matching Front/Backend and A/B templates. Failed smoke analysis prevents
advancing to the pause; do not bypass it without investigating the error.
`oc argo rollouts promote` resumes past the final pause, and Argo Rollouts
finishes moving traffic to the new stable revision. Do not use `--full`
unless intentionally bypassing outstanding gates.

## Select independent traffic paths

| Header | Choices | Effect |
| --- | --- | --- |
| `x-app-target` | `a`, `b` | A/B stack, ingress routing |
| `x-front-track` | `stable`, `canary`, *absent* | Force Front or use weighted 50/50 |
| `x-backend-track` | `stable`, `canary`, *absent* | Force Backend or use weighted 50/50 |
| `x-db-track` | `stable`, `canary` | Backend selects MariaDB active/preview Service over TCP |

**The MySQL protocol has no HTTP headers.** MariaDB blue-green switching is
performed by Argo Rollouts changing the Service selector. When `x-db-track` is
set, the HTTP Backend chooses `mariadb-a-stable`/`mariadb-a-canary`, or the
B equivalents, and then opens a native MySQL connection. An Istio HTTP
VirtualService cannot inspect the SQL connection to choose a database.

Only requests **without** forced Front/Backend track headers are split 50/50
by Istio. The 50% weight is configured on the `primary`, `secondary`, and
`backend-{a,b}-default` routes of the existing VirtualServices; do not edit
those route weights directly, because the Rollouts controller owns them.

## Example: Application A

```bash
# Commit BLUE baseline / prepare.
INSTANCE=a bash scripts/full-multi.sh prepare

# Commit YELLOW and stop after successful 50% smoke analysis.
INSTANCE=a bash scripts/full-multi.sh start

# Inspect; both Front/Backend must show Paused at currentStepIndex=6.
oc -n full-multi get rollout front-a backend-a \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,STEP:.status.currentStepIndex'

# Force GREEN/YELLOW candidate at either HTTP tier, DB stays active.
HOST="$(oc -n full-multi get route full-multi -o jsonpath='{.spec.host}')"
curl -kfsS -H 'x-app-target: a' -H 'x-front-track: canary' \
  -H 'x-backend-track: stable' -H 'x-db-track: stable' \
  "https://$HOST/api/books"

# Allow weighted Front/Backend: omit both forced track headers.
INSTANCE=a REQUESTS=100 FRONT_TRACK=weighted BACKEND_TRACK=weighted \
  DB_TRACK=stable bash scripts/full-multi.sh traffic

# Complete database blue-green and HTTP canary promotions.
INSTANCE=a bash scripts/full-multi.sh promote
```

Repeat with `INSTANCE=b` to advance Application B independently.
For manual per-tier promotion, use `oc argo rollouts promote front-a -n full-multi`
and `oc argo rollouts promote backend-a -n full-multi`; MariaDB promotion is
`oc argo rollouts promote mariadb-a -n full-multi` after preview checks.

## Preconditions and cautions

- The existing Argo CD Application was previously reported **OutOfSync** and
  **Degraded**. Confirm it actually reconciles to the latest Git revision
  before invoking `start`. Git commits alone do not update live VirtualServices.
- The two Backend VirtualServices must target their TCP **Service port 5000**
  for HTTP; the previous live configuration incorrectly used port 80.
- A 50/50 weight affects **HTTP requests**, not SQL traffic percentages. With
  one stable and one canary Pod, Istio can still split HTTP at 50%.
- MariaDB data uses `emptyDir`; active and preview databases are independent
  and ephemeral. Promoting the preview can discard the previous active data.
- Do not force promote a degraded Rollout or delete database Pods to reset
  history. Fix AnalysisRun failures and Services first.
- The image is already `full-multi-tracing-v2`; this patch changes no Go
  source, container tag, Secrets, or MariaDB Rollout.
