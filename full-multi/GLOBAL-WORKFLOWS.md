# full-multi: global setup and prepare

## Ambient platform prerequisites

The demo tracks `multi-ambient`. Its shared Istio control plane, ambient CNI,
ZTunnel, Gateway API, Rollouts plugin and observability stack must already be
installed by their owners. The sidecar-oriented `SM/v3/v3.5` installer is not
run by the ambient setup workflow. See [AMBIENT.md](AMBIENT.md) before deployment.
The original `full-multi` requirements still apply: OpenShift GitOps and
Rollouts CRDs, a cluster-scoped Rollouts controller and CLI, appropriate
cluster and GitHub access, a StorageClass, the existing application images,
and the local `oc`, Git, Python, Bash, `jq`, `curl`, and `skopeo` tools. The
ambient HTTPRoutes also require the Gateway API traffic plugin on that same
controller. Existing mesh and observability services are reused.

For OVN-Kubernetes ambient probes, the cluster must use local gateway mode.
Inspect and, when the cluster-wide routing change is authorized, enable it with:

```bash
python3 scripts/full-multi-ambient-network.py --check
python3 scripts/full-multi-ambient-network.py --apply
```

The apply command changes `routingViaHost` for the whole cluster, then waits
for OVN's effective local mode and daemonsets to reconcile. Network traffic may
be disrupted during the transition. The mode is not changed by the regular
GitOps installer or application setup.

Install the missing GitOps prerequisites separately, using the same script
for an initial installation and after an interruption:

```bash
python3 scripts/full-multi-install-prereqs.py --check
python3 scripts/full-multi-install-prereqs.py --apply
```

The installer uses the approved `latest` GitOps Operator channel, enables the
operator's OpenShift `gitops-plugin` console UI, reuses or creates a single
cluster-scoped RolloutManager, configures the Gateway API plugin, and grants
its service account only the required permissions in `multi-ambient`. It also
grants the GitOps application controller namespace-scoped Gateway access. It
preserves unrelated console plugins and traffic plugins, and refuses conflicting
existing resources. It does not install Service Mesh, OpenTelemetry or Tempo.
After installation, verify the plugin has loaded in the controller logs; an
Available RolloutManager alone does not prove that the plugin can run.

For disposable demo credentials, provision new Secrets once with:

```bash
bash scripts/full-multi-secrets.sh --generate-demo
```

It uses random values, does not print them, and leaves existing Secrets alone.
The standard `FULL_MULTI_DB_PASSWORD` and `FULL_MULTI_DB_ROOT_PASSWORD` inputs
remain available without `--generate-demo`. Partial existing Secrets are refused.

```bash
bash scripts/full-multi-global-setup.sh
bash scripts/full-multi-global-setup.sh --apply --push
```

Setup validates the ambient prerequisites, provisions the four databases and
credentials if needed, checks their isolation and grants, and establishes a
Blue baseline. It commits and pushes workflow changes to `multi-ambient`.
The setup uses existing database Secrets, or the standard credential inputs for
first provisioning. It never removes the PVCs.

## Global prepare: any *valid* Rollout lifecycle state

```bash
cd ~/Documents/POCs/argo-sm
bash scripts/full-multi-global-prepare.sh
bash scripts/full-multi-global-prepare.sh --apply --push
```

This is a **deliberate reset**, not a status check:

1. Verify the GitOps source, service controller, four isolated PVCs and SELECT-only application privileges.
2. Abort any in-progress/paused candidate revisions (the application rolls back toward its previously healthy active ReplicaSet). Does not delete data.
3. Set all six Rollout templates to deterministic Blue/English baseline via `scripts/full-multi-spec.py set`, commit and push only six Rollout YAMLs to `multi-ambient` when necessary.
4. Observe GitOps revision, and complete **only confirmed Blue** pauses with native Argo Rollouts promotion. Green/YELLOW candidates are never promoted as part of a prepare reset.
5. Require all six Blue Rollouts Healthy and test the A/B read-only HTTP-to-SQL data paths.
6. Delete completed AnalysisRuns and non-current ReplicaSets with the existing strict history reset implementation; leave the four StatefulSets/PVCs intact.
7. Re-run E2E. Success means ready for `start` on both applications.

The reset handles previously paused Green, promoted Green, paused Canary, Healthy Blue, and previously aborted updates **when the cluster is otherwise functional**. It fails closed on unrecognized Degraded state, missing controller, bad storage, Git divergence or unhealthy infrastructure: no software can safely promise to recover arbitrary failures by silently deleting resources.

The reset is a sequence of Git, database and cluster operations, not a single
transaction. After an interruption, inspect the reported error and rerun only
after resolving it. A known aborted Rollout is retried only after its live
template is verified Blue and its generation observed. If a workflow commit was
created but its push failed, rerunning requires the workflow's local pending
ref and verifies the commit's parent, message, files and exact template changes
before retrying the push. An interruption before the ref is recorded, or other
Git divergence, requires manual inspection.

Do not run global prepare during a demonstration if you intend to keep the Green preview paused; `prepare` intentionally returns both apps to Blue. To reset only A or B, use `INSTANCE=a|b scripts/full-multi prepare`, backed by the same verified reset implementation. That selected form changes only three rollout manifests, aborts only the selected candidate and retains the other app's current preview; the four SQL volumes remain untouched.

## Start the demo and verify it stays paused

```bash
# One command starts both A and B, and automatically runs the paused CRUD
# gate, four-PVC isolation check, and Blue/Green HTTP-to-SQL E2E checks.
bash scripts/full-multi.sh start
```

No separate `full-multi-crud-pause-gate.py` command is necessary: the per-app
checks run inside each `start` operation, and the final cross-app gate runs
inside the combined `start` entry point. `INSTANCE=a|b bash scripts/full-multi.sh
start` remains available for individual-app diagnostics.

All four SQL databases remain independently available and persistent. Both CRUD Rollouts must be Paused, with Blue active/Green preview serving both 20-book datasets for the entire demonstration. `promote` still works on CRUD, Front and Backend **only when explicitly invoked**:

```bash
INSTANCE=a bash scripts/full-multi.sh promote
INSTANCE=b bash scripts/full-multi.sh promote
```

After a deliberate promotion, Blue endpoints may be scaled down; global `prepare` then restores the Blue baseline for the next demonstration.

## Read-only status / verification

```bash
python3 scripts/full-multi-global.py status
python3 scripts/full-multi-e2e.py --state blue
python3 scripts/full-multi-e2e.py --state paused
python3 -m unittest discover -s tests -p test_full_multi_global.py
```

There is no automatic GitHub write action through ChatGPT. After reviewing the installed scripts, commit and push them using your own `git` credentials. **Never commit the SM `.env`, generated Tempo secrets, or database credentials.**

The read-only E2E entry point checks the active Gateway and HTTPRoutes, GitOps
sync, actual configured canary weights, the existing database and rollout
gates, and the Blue data paths. In the paused state it also checks every
forced Frontend, Backend and database track. It does not establish Tempo trace
retrieval or Kiali scrape-target health; verify those separately.

If a sync is blocked specifically by the two known Gateway create-permission
failures, rerun the prerequisite installer to correct permissions. The guarded
`full-multi-retry-gateway-sync.py` can then terminate and resubmit only the
recognized failed operation for the exact revision; run its `--revision` form
first as a read-only check and use `--apply` for each required recovery stage.
It refuses unrelated failures or revisions.
