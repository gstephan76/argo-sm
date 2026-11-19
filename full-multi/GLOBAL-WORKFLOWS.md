# full-multi: one-command preparation

## Install and prepare both applications

The canonical command for initial installation and subsequent preparation is:

```bash
scripts/full-multi prepare
```

Without `INSTANCE`, it prepares A and B and owns the complete sequence:

1. Validate the local tools, Git branch, clean checkout and published revision.
2. Run the read-only OVN check, request `routingViaHost=true` if needed, and wait for effective local gateway mode and stable daemonsets.
3. Check and install/reconcile GitOps, the cluster-scoped Rollouts controller, console plugin, Gateway API traffic plugin and namespace RBAC.
4. Generate missing disposable demo credentials while preserving existing Secrets.
5. Provision the four databases, verify distinct PVCs and reconcile SELECT-only application grants.
6. Publish Blue before creating a fresh GitOps Application, synchronize it, establish all six Healthy Blue Rollouts, run E2E and clear eligible history.

Each stage must succeed before the next starts. Invoking `prepare` authorizes
cluster-wide OVN changes and GitOps commits/pushes; network reconciliation can
temporarily disrupt traffic. The helper scripts are implementation steps and
do not need to be run separately.

The demo tracks `multi-ambient`. The cluster must already provide ambient
Istio/CNI/ZTunnel, Gateway API and the observability stack, along with a default
StorageClass and access to the existing application images. The client needs
the Rollouts CLI, cluster and GitHub access, and `oc`, Git, Python, Bash, `jq`,
`curl` and `skopeo`. Prepare installs the missing GitOps/Rollouts prerequisites
using the approved `latest` GitOps Operator channel and preserves unrelated
plugins. It reuses Service Mesh, OpenTelemetry and Tempo. See [AMBIENT.md](AMBIENT.md).

The credential step uses `--generate-demo`: random values are not printed and
existing Secrets are left alone. An interrupted pair is completed by generating only
the missing app's Secret, provided that app has no workloads or PVCs. Existing
Secrets are never read for their values or overwritten. If storage or workloads
exist for an app whose Secret is missing, restore its original credentials;
the script refuses to generate replacements. The lower-level Secrets helper
still supports explicit credential inputs without `--generate-demo`.

### Resilience and rerun contract

The provisioning commands must tolerate reruns after completed stages and
recoverable interruptions. The setup path reuses `scripts/full_multi_oc.py`:
transient GET failures have up to three attempts with bounded backoff, including
DNS, connection and API availability failures. `FULL_MULTI_OC_READ_ATTEMPTS`
can select 1–5 attempts, and `FULL_MULTI_OC_REQUEST_TIMEOUT` can select a 1–120
second request timeout (default 30 seconds). Authentication and authorization
failures are reported immediately.

Writes are reconciled from live evidence. The prerequisite installer retries a
patch at most three times when only status, resourceVersion or managedFields
changed; it preserves the version guard and refuses configuration or identity
changes. After an uncertain create response it accepts only a matching live
resource. Operator-owned controllers and service accounts are awaited rather
than assumed to exist as soon as their CRDs are established. OVN requests guard
the network UID and inspected spec, and verify the live setting after an
uncertain response before waiting for effective network readiness.

After resolving any reported error, rerun `scripts/full-multi prepare`.
Genuine conflicts, missing credentials for existing
data, unrelated Git changes and unverified pending commits require inspection;
they are never repaired by overwriting state. Repeated preparation still deliberately
establishes Blue and clears eligible history, so use it only for that intended
operation. Prepare requires a clean checkout synchronized with
`origin/multi-ambient`; commit and publish reviewed script changes before it.

## Global prepare: any _valid_ Rollout lifecycle state

```bash
cd ~/Documents/POCs/argo-sm
scripts/full-multi prepare
```

This is a **deliberate reset**, not a status check:

1. Verify the GitOps source, service controller, four isolated PVCs and SELECT-only application privileges.
2. Abort any in-progress/paused candidate revisions (the application rolls back toward its previously healthy active ReplicaSet). Does not delete data.
3. Set all six Rollout templates to deterministic Blue/English baseline via `scripts/full-multi-spec.py set`, commit and push only six Rollout YAMLs to `multi-ambient` when necessary.
4. Observe GitOps revision, and complete **only confirmed Blue** pauses with native Argo Rollouts promotion. Green/YELLOW candidates are never promoted as part of a prepare reset.
5. Require all six Blue Rollouts Healthy and test the A/B read-only HTTP-to-SQL data paths.
6. Delete completed AnalysisRuns and non-current ReplicaSets with the existing strict history reset implementation; leave the four StatefulSets/PVCs intact.
7. Re-run E2E. Success means ready for `start` on both applications.

The reset handles previously paused Green, promoted Green, paused Canary, Healthy Blue, and previously aborted updates **when the cluster is otherwise functional**. It fails closed on unrecognized Degraded state, unavailable storage, Git divergence or unhealthy shared infrastructure. It never deletes data to recover an unexpected failure.

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

After reviewing the installed scripts, commit and push them using your own `git` credentials. **Never commit the SM `.env`, generated Tempo secrets, or database credentials.**

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
